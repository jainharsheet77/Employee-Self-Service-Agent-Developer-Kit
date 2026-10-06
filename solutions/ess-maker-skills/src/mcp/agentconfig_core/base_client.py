# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Neutral AgentConfiguration client core shared by the landing-page and planner MCPs.

This module owns everything both AgentConfiguration MCP surfaces need and neither should
re-implement: bearer-token acquisition, the JWT claim decode, a single shared
httpx session, and the retrying ``_request``. ``AgentConfigClient`` (landing
page) and ``PlannerClient`` (planner) both inherit ``AgentConfigBaseClient``, each with its
own base URL and logger name.

Token acquisition, in priority order:
  1. AGENTCONFIG_ACCESS_TOKEN_FILE / AGENTCONFIG_ACCESS_TOKEN.
  2. MSAL public-client sign-in with a local form_post callback.

The tenant ID comes from the resolved token's ``tid`` claim and the caller
object id from ``oid``. The API still validates the token and enforces
authorization; the client decodes claims only to address tenant-scoped routes
and to scope "for the caller" queries to the signed-in principal.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import http.server
import json
import logging
import os
import random
import stat
import sys
import threading
import urllib.parse
import uuid
import webbrowser
from typing import Any, Optional

import httpx


logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_CLIENT_ID = "417219b4-3a7d-42a2-bdb1-972bd8281a02"
_SCOPE = ["https://substrate.office.com/weve/.default"]
_AUTHORITY = "https://login.microsoftonline.com/organizations"
# Derived from this shared-core module's own location so every AgentConfiguration MCP
# (landing-page, planner) shares ONE MSAL cache and ONE interactive sign-in.
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_STATE_DIR = os.path.join(_CORE_DIR, ".local")
_TOKEN_CACHE_PATH = os.path.join(_LOCAL_STATE_DIR, "msal_token_cache.bin")
# How many times a single request may re-authenticate after a 401 before giving
# up: one silent force-refresh, then one explicit interactive sign-in.
_MAX_REAUTH_ATTEMPTS = 2


class AgentConfigApiError(RuntimeError):
    """Raised when the production AgentConfiguration API rejects a request."""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def _read_token_file(path: str) -> str:
    """Read and validate a bearer token from ``AGENTCONFIG_ACCESS_TOKEN_FILE``."""
    if not os.path.isfile(path):
        raise ValueError(
            f"AGENTCONFIG_ACCESS_TOKEN_FILE={path!r} does not exist"
        )
    with open(path, "r", encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise ValueError(
            f"AGENTCONFIG_ACCESS_TOKEN_FILE={path!r} is empty"
        )
    return token


def _resolve_token_with_source() -> tuple[str, str]:
    """Resolve a token and record HOW it was obtained, so a later 401 knows
    whether it can renew in-process. ``"file"``/``"env"`` tokens are injected
    out-of-band and cannot be refreshed here (the caller surfaces an explicit
    sign-in error); an ``"msal"`` token can be force-refreshed or re-acquired
    through an interactive sign-in. Priority is unchanged: token file, then env
    var, then interactive MSAL."""
    token_file = os.environ.get("AGENTCONFIG_ACCESS_TOKEN_FILE", "")
    if token_file:
        return _read_token_file(token_file), "file"

    token = os.environ.get("AGENTCONFIG_ACCESS_TOKEN", "").strip()
    if token:
        return token, "env"

    return acquire_token_msal_interactive(), "msal"


def _resolve_token() -> str:
    """Resolve a token without its source (see :func:`_resolve_token_with_source`)."""
    return _resolve_token_with_source()[0]


class _FormPostCaptureHandler(http.server.BaseHTTPRequestHandler):
    """Capture one OAuth form_post callback from the local loopback listener."""

    captured: dict[str, str] = {}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        params = urllib.parse.parse_qs(body)
        _FormPostCaptureHandler.captured = {
            key: values[0] for key, values in params.items() if values
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body>Signed in. You can close this tab.</body></html>"
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _load_msal_cache() -> Any:
    import msal

    cache = msal.SerializableTokenCache()
    if os.path.exists(_TOKEN_CACHE_PATH):
        with open(_TOKEN_CACHE_PATH, "r", encoding="utf-8") as handle:
            cache.deserialize(handle.read())
    return cache


def _save_msal_cache(cache: Any) -> None:
    if not cache.has_state_changed:
        return

    os.makedirs(_LOCAL_STATE_DIR, exist_ok=True)
    try:
        os.chmod(_LOCAL_STATE_DIR, 0o700)
    except OSError:
        pass

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(_TOKEN_CACHE_PATH, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(cache.serialize())
    finally:
        try:
            os.chmod(_TOKEN_CACHE_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def acquire_token_msal_interactive() -> str:
    """Acquire a delegated AgentConfiguration token through cached or interactive MSAL auth."""
    import msal

    cache = _load_msal_cache()
    app = msal.PublicClientApplication(
        _CLIENT_ID,
        authority=_AUTHORITY,
        token_cache=cache,
    )

    accounts = app.get_accounts()
    result = app.acquire_token_silent(_SCOPE, account=accounts[0]) if accounts else None
    if not result or "access_token" not in result:
        result = _acquire_token_interactive_form_post(app)

    _save_msal_cache(cache)
    if "access_token" not in result:
        error = result.get("error", "unknown_error")
        description = result.get("error_description", "")
        raise ValueError(f"MSAL sign-in failed ({error}): {description}")
    return result["access_token"]


def acquire_token_msal_refreshed(*, force_interactive: bool = False) -> str:
    """Re-acquire a delegated AgentConfiguration token after a 401.

    Unless ``force_interactive``, first force-refresh silently
    (``force_refresh=True`` bypasses the cached — now server-rejected — access
    token and spends the refresh token, no browser). Fall back to an interactive
    sign-in when the refresh token is gone, interaction is required, or
    ``force_interactive`` is set — that interactive prompt is the explicit login
    the user is asked for before the planner can progress past a dead session.
    """
    import msal

    cache = _load_msal_cache()
    app = msal.PublicClientApplication(
        _CLIENT_ID,
        authority=_AUTHORITY,
        token_cache=cache,
    )

    result = None
    if not force_interactive:
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(
                _SCOPE, account=accounts[0], force_refresh=True
            )
    if not result or "access_token" not in result:
        result = _acquire_token_interactive_form_post(app)

    _save_msal_cache(cache)
    if "access_token" not in result:
        error = result.get("error", "unknown_error")
        description = result.get("error_description", "")
        raise ValueError(f"MSAL sign-in failed ({error}): {description}")
    return result["access_token"]


def _acquire_token_interactive_form_post(app: Any) -> dict[str, Any]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _FormPostCaptureHandler)
    redirect_uri = f"http://localhost:{server.server_port}"
    flow = app.initiate_auth_code_flow(
        scopes=_SCOPE,
        redirect_uri=redirect_uri,
        response_mode="form_post",
        prompt="select_account",
    )

    _FormPostCaptureHandler.captured = {}
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    # This runs inside stdio MCP servers, where stdout is reserved for JSON-RPC
    # frames — a stray line there corrupts the protocol stream and disconnects
    # the client. Send the human-facing sign-in notice to stderr instead.
    print(
        f"Opening browser for AgentConfiguration sign-in ({redirect_uri}) ...",
        file=sys.stderr,
        flush=True,
    )
    webbrowser.open(flow["auth_uri"])
    thread.join(timeout=300)
    server.server_close()

    if not _FormPostCaptureHandler.captured:
        return {
            "error": "timeout",
            "error_description": "No sign-in callback received within 300 seconds.",
        }

    return app.acquire_token_by_auth_code_flow(
        flow,
        _FormPostCaptureHandler.captured,
    )


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT's payload segment without verifying its signature.

    The API validates the token and enforces authorization; the client decodes
    claims only to address tenant-scoped routes (``tid``) and to scope "for the
    caller" queries to the signed-in principal (``oid``).
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(
            "AGENTCONFIG_ACCESS_TOKEN does not look like a JWT "
            "(expected three dot-separated segments)"
        )
    payload_segment = parts[1]
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(
            f"Could not decode AGENTCONFIG_ACCESS_TOKEN payload: {error}"
        ) from error


def _decode_tenant_id_from_jwt(token: str) -> str:
    """Decode and validate the tenant ID (``tid``) used to address the route."""
    tenant_id = _decode_jwt_payload(token).get("tid")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("AGENTCONFIG_ACCESS_TOKEN payload has no 'tid' claim")
    try:
        return str(uuid.UUID(tenant_id))
    except ValueError as error:
        raise ValueError(
            "AGENTCONFIG_ACCESS_TOKEN payload has an invalid 'tid' claim"
        ) from error


def _decode_object_id_from_jwt(token: str) -> Optional[str]:
    """Best-effort decode of the caller's Entra object id (``oid`` claim).

    Used to scope "tasks for the caller" queries to the signed-in principal
    without taking the identity as a tool argument. Returns ``None`` when the
    token is opaque or carries no ``oid`` claim.
    """
    try:
        payload = _decode_jwt_payload(token)
    except ValueError:
        return None
    object_id = payload.get("oid")
    if isinstance(object_id, str) and object_id:
        return object_id
    return None


class AgentConfigBaseClient:
    """Neutral AgentConfiguration client core shared by the landing-page and planner MCPs.

    Owns everything both surfaces need and neither should re-implement: MSAL/
    bearer token acquisition, the JWT claim decode, a single shared httpx
    session, and the retrying ``_request``. Subclasses supply their own base URL
    and logger name and layer their domain routes on top; they never duplicate
    auth or transport. ``AgentConfigApiError`` and ``_TOKEN_CACHE_PATH`` live
    here so both MCPs raise one error type and share one interactive sign-in.
    """

    def __init__(
        self,
        *,
        base_url: str,
        logger_name: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._logger = logging.getLogger(logger_name)
        self._token, self._token_source = _resolve_token_with_source()
        self.tenant_id = _decode_tenant_id_from_jwt(self._token)
        self.max_retries = 3
        self.timeout = 30.0
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()
        # Serializes 401 recovery so a burst of concurrent 401s triggers at most
        # one re-authentication / interactive sign-in.
        self._auth_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} base_url={self.base_url!r} "
            f"tenant_id={self.tenant_id!r}>"
        )

    def _transform_response(self, payload: Any) -> Any:
        """Surface-specific response key transform; identity in the neutral core.

        The landing-page client overrides this to apply its PascalCase→camelCase
        conversion. The planner and role surfaces keep the default because their
        bodies carry user keys that must not be rewritten.
        """
        return payload

    def _format_api_error(self, body: Any, status_code: int) -> str:
        """Build an ``AgentConfigApiError`` message from an error response body.

        The neutral core surfaces only the top-level ``Code`` and ``Message``
        from the AgentConfiguration error envelope. A server (e.g. the planner)
        may override this to surface additional actionable detail — such as the
        ``Target`` field and the nested ``Details[]`` validation entries — that
        the caller (or the agent) needs to self-correct.
        """
        code = ""
        message = ""
        if isinstance(body, dict):
            code_candidate = body.get("Code")
            if isinstance(code_candidate, str):
                code = code_candidate
            message_candidate = body.get("Message")
            if isinstance(message_candidate, str):
                message = message_candidate
        if not code:
            code = "HttpError"
        if not message:
            message = f"HTTP {status_code}"
        return f"{code}: {message}"

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout,
                    verify=True,
                    transport=self._transport,
                    follow_redirects=False,
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _reset_client(self) -> None:
        """Drop the cached httpx session so the next :meth:`_ensure_client`
        rebuilds it carrying the refreshed bearer in its Authorization header.
        Used after a 401 re-auth replaces :attr:`_token`."""
        async with self._client_lock:
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

    def _on_token_refreshed(self) -> None:
        """Hook: re-derive any token-scoped state a subclass caches after a 401
        re-auth. The neutral core keeps only :attr:`tenant_id` (already re-decoded
        by :meth:`_reauthenticate`); subclasses override to refresh their own —
        e.g. the planner's caller ``oid``."""

    def _acquire_refreshed_token(self, force_interactive: bool) -> Optional[str]:
        """Blocking token re-acquisition behind :meth:`_reauthenticate` (run in a
        worker thread). An ``msal`` token is force-refreshed, escalating to an
        interactive sign-in when the refresh token is gone; a token injected via
        ``AGENTCONFIG_ACCESS_TOKEN`` is fixed for the process so this returns
        ``None``; a token *file* is re-read in case an external broker rotated it,
        returning ``None`` when it is unchanged or unreadable. ``None`` tells the
        caller re-auth is impossible so it surfaces an explicit sign-in error."""
        if self._token_source == "msal":
            return acquire_token_msal_refreshed(force_interactive=force_interactive)
        if self._token_source == "file":
            path = os.environ.get("AGENTCONFIG_ACCESS_TOKEN_FILE", "")
            try:
                return _read_token_file(path)
            except (ValueError, OSError):
                return None
        return None

    async def _reauthenticate(self, *, force_interactive: bool, seen_token: str) -> bool:
        """Recover from a 401 by replacing the expired/revoked bearer and rebuilding
        the session so the retry carries a live token.

        Returns ``True`` when a usable new token is in place — obtained here, or
        already refreshed by a concurrent caller — and ``False`` when the token was
        injected out-of-band (env var / file) and cannot be renewed in-process, so
        the caller surfaces an explicit sign-in error. Serialized on
        :attr:`_auth_lock` so a burst of concurrent 401s triggers at most one
        sign-in; the blocking acquisition runs in a thread so it never stalls the
        event loop (the interactive flow can wait minutes on the browser)."""
        async with self._auth_lock:
            if self._token != seen_token:
                # A concurrent caller already refreshed while we waited on the lock.
                return True
            loop = asyncio.get_running_loop()
            new_token = await loop.run_in_executor(
                None, self._acquire_refreshed_token, force_interactive
            )
            if not new_token or new_token == self._token:
                return False
            self._token = new_token
            self.tenant_id = _decode_tenant_id_from_jwt(self._token)
            self._on_token_refreshed()
            await self._reset_client()
            return True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        transform_payload: bool = True,
        idempotent: Optional[bool] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute a request with bounded retry for transient responses.

        ``transform_payload`` controls the landing-page camelCase/PascalCase key
        conversion applied to the response body. It defaults to ``True`` so the
        EmployeeAgents surface is unchanged; the planner and role surfaces pass
        ``False`` because their responses carry user keys that must not be
        rewritten.

        ``idempotent`` gates whether an *ambiguous* transient failure — a 502/
        503/504 gateway error or a network ``RequestError`` that may have landed
        server-side after committing — is safe to replay. It defaults to
        retry-safe (``True``) so the shared default never silently drops retry
        coverage as new call sites are added; a call site that would genuinely
        duplicate on replay (an unkeyed create) passes ``idempotent=False`` to
        opt out, and that unsafe create is surfaced instead of retried so a
        committed-but-unacknowledged POST is never duplicated. A 429 is always
        retried because the service rejects it before doing any work.

        A **401** is handled separately from the transient codes: a stale or
        revoked bearer makes every call fail with the *same* dead token (the
        session caches it), so a plain retry can never clear it. The request
        re-authenticates in place — one silent force-refresh, then an explicit
        interactive sign-in — and replays once. When the token was injected
        out-of-band (env var / file) and cannot be renewed in-process, the call
        surfaces an explicit "sign in again" error instead of looping on 401s.
        """
        # Default to retry-safe so the landing-page surface keeps its original
        # retry-on-transient behavior; only call sites that would genuinely
        # duplicate on replay (unkeyed creates) opt out with idempotent=False.
        retry_safe = True if idempotent is None else idempotent
        last_error: Optional[Exception] = None
        reauth_attempts = 0
        for attempt in range(self.max_retries):
            client = await self._ensure_client()
            request_token = self._token
            try:
                response = await client.request(method, path, **kwargs)
                if response.status_code == 401:
                    if reauth_attempts < _MAX_REAUTH_ATTEMPTS and (
                        await self._reauthenticate(
                            force_interactive=reauth_attempts >= 1,
                            seen_token=request_token,
                        )
                    ):
                        reauth_attempts += 1
                        continue
                    raise AgentConfigApiError(
                        "Unauthorized (HTTP 401): the AgentConfiguration sign-in "
                        "has expired or was revoked and could not be renewed "
                        "automatically. Sign in again, then retry the planner "
                        "action and complete the browser sign-in when prompted.",
                        http_status=401,
                    )
                if response.status_code == 429 or response.status_code in (
                    502,
                    503,
                    504,
                ):
                    if response.status_code != 429 and not retry_safe:
                        # An ambiguous gateway failure on a non-idempotent
                        # request (typically an unkeyed create) may already have
                        # committed server-side; replaying it risks a duplicate,
                        # so surface it instead of retrying.
                        response.raise_for_status()
                    wait = (2**attempt) + random.uniform(0, 1)
                    last_error = AgentConfigApiError(
                        f"Transient HTTP {response.status_code}"
                    )
                    self._logger.warning(
                        "Retryable HTTP %d (attempt %d/%d), waiting %.1fs",
                        response.status_code,
                        attempt + 1,
                        self.max_retries,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                if response.status_code == 204:
                    return {"success": True}
                payload = response.json()
                return (
                    self._transform_response(payload)
                    if transform_payload
                    else payload
                )

            except httpx.HTTPStatusError as error:
                try:
                    body = error.response.json()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = None
                raise AgentConfigApiError(
                    self._format_api_error(body, error.response.status_code),
                    http_status=error.response.status_code,
                ) from error

            except httpx.RequestError as error:
                last_error = error
                if retry_safe and attempt < self.max_retries - 1:
                    await asyncio.sleep((2**attempt) + random.uniform(0, 1))
                    continue
                raise

        raise AgentConfigApiError(f"Maximum retries exceeded: {last_error}")
