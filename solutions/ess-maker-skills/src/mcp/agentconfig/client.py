# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Production AgentConfiguration REST client with bearer authentication.

Token acquisition, in priority order:
  1. AGENTCONFIG_ACCESS_TOKEN_FILE / AGENTCONFIG_ACCESS_TOKEN.
  2. MSAL public-client sign-in with a local form_post callback.

The tenant ID comes from the resolved token's ``tid`` claim. The API still
validates the token and enforces authorization; the client decodes the claim
only to address the tenant-scoped EmployeeAgents route.
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
import threading
import urllib.parse
import uuid
import webbrowser
from typing import Any, Optional

import httpx


logger = logging.getLogger("ess-landing-page-config")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_CLIENT_ID = "417219b4-3a7d-42a2-bdb1-972bd8281a02"
_SCOPE = ["https://substrate.office.com/weve/.default"]
_AUTHORITY = "https://login.microsoftonline.com/organizations"
DEFAULT_AGENTCONFIG_BASE_URL = "https://substrate.office.com/weveb2/api/v1.1"
_AGENTCONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_STATE_DIR = os.path.join(_AGENTCONFIG_DIR, ".local")
_TOKEN_CACHE_PATH = os.path.join(_LOCAL_STATE_DIR, "msal_token_cache.bin")
_MAX_TITLE_ID_LENGTH = 256
_MAX_SEARCH_LENGTH = 256


class AgentConfigApiError(RuntimeError):
    """Raised when the production AgentConfiguration API rejects a request."""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def _validate_title_id(title_id: str) -> str:
    if not isinstance(title_id, str):
        raise ValueError("titleId must be a string")
    if not title_id or title_id != title_id.strip():
        raise ValueError(
            "titleId must be a non-empty string without surrounding whitespace"
        )
    if len(title_id) > _MAX_TITLE_ID_LENGTH:
        raise ValueError(
            f"titleId must not exceed {_MAX_TITLE_ID_LENGTH} characters"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in title_id):
        raise ValueError("titleId must not contain control characters")
    return title_id


def _encode_odata_key(value: str) -> str:
    escaped = value.replace("'", "''")
    return urllib.parse.quote(escaped, safe="")


def _convert_key_case(value: Any, *, upper: bool) -> Any:
    """Recursively convert the first character of JSON object keys."""
    if isinstance(value, list):
        return [_convert_key_case(item, upper=upper) for item in value]
    if not isinstance(value, dict):
        return value

    converted: dict[str, Any] = {}
    for key, item in value.items():
        if key and key[0].isalpha():
            first = key[0].upper() if upper else key[0].lower()
            converted_key = first + key[1:]
        else:
            converted_key = key
        converted[converted_key] = _convert_key_case(item, upper=upper)
    return converted


def _to_api_payload(value: Any) -> Any:
    return _convert_key_case(value, upper=True)


def _to_tool_payload(value: Any) -> Any:
    return _convert_key_case(value, upper=False)


def _resolve_token() -> str:
    """Resolve a token without writing it to logs or MCP configuration."""
    token_file = os.environ.get("AGENTCONFIG_ACCESS_TOKEN_FILE", "")
    if token_file:
        if not os.path.isfile(token_file):
            raise ValueError(
                f"AGENTCONFIG_ACCESS_TOKEN_FILE={token_file!r} does not exist"
            )
        with open(token_file, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
        if not token:
            raise ValueError(
                f"AGENTCONFIG_ACCESS_TOKEN_FILE={token_file!r} is empty"
            )
        return token

    token = os.environ.get("AGENTCONFIG_ACCESS_TOKEN", "").strip()
    if token:
        return token

    return acquire_token_msal_interactive()


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
    """Acquire a delegated Weve token through cached or interactive MSAL auth."""
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

    print(f"Opening browser for AgentConfiguration sign-in ({redirect_uri}) ...")
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


def _decode_tenant_id_from_jwt(token: str) -> str:
    """Decode and validate the tenant ID used to address the API route."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(
            "AGENTCONFIG_ACCESS_TOKEN does not look like a JWT "
            "(expected three dot-separated segments)"
        )

    payload_segment = parts[1]
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(padded)
        payload = json.loads(payload_bytes)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(
            f"Could not decode AGENTCONFIG_ACCESS_TOKEN payload: {error}"
        ) from error

    tenant_id = payload.get("tid")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("AGENTCONFIG_ACCESS_TOKEN payload has no 'tid' claim")
    try:
        return str(uuid.UUID(tenant_id))
    except ValueError as error:
        raise ValueError(
            "AGENTCONFIG_ACCESS_TOKEN payload has an invalid 'tid' claim"
        ) from error


class AgentConfigClient:
    """Async client for production EmployeeAgents list/search/create/get/PATCH."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        base_url = os.environ.get(
            "AGENTCONFIG_BASE_URL",
            DEFAULT_AGENTCONFIG_BASE_URL,
        )

        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "AGENTCONFIG_BASE_URL must be an HTTPS URL without credentials, "
                "a query, or a fragment"
            )

        self.base_url = base_url.rstrip("/")
        self._token = _resolve_token()
        self.tenant_id = _decode_tenant_id_from_jwt(self._token)
        self.max_retries = 3
        self.timeout = 30.0
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return (
            f"<AgentConfigClient base_url={self.base_url!r} "
            f"tenant_id={self.tenant_id!r}>"
        )

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

    def _collection_path(self) -> str:
        return f"tenants('{self.tenant_id}')/EmployeeAgents"

    def _agent_path(self, title_id: str) -> str:
        encoded = _encode_odata_key(_validate_title_id(title_id))
        return f"{self._collection_path()}('{encoded}')"

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
        server-side after committing — is safe to replay. When ``None`` it is
        inferred: safe for read methods and for any mutation carrying an
        ``If-Match`` or ``Idempotency-Key`` header, unsafe otherwise. An unsafe
        create is surfaced instead of retried so a committed-but-unacknowledged
        POST is never silently duplicated. A 429 is always retried because the
        service rejects it before doing any work.
        """
        if idempotent is None:
            request_headers = kwargs.get("headers") or {}
            retry_safe = method.upper() in ("GET", "HEAD", "OPTIONS") or any(
                name.lower() in ("if-match", "idempotency-key")
                for name in request_headers
            )
        else:
            retry_safe = idempotent
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            client = await self._ensure_client()
            try:
                response = await client.request(method, path, **kwargs)
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
                    logger.warning(
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
                return _to_tool_payload(payload) if transform_payload else payload

            except httpx.HTTPStatusError as error:
                code = ""
                message = ""
                try:
                    body = error.response.json()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = None
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
                    message = f"HTTP {error.response.status_code}"
                raise AgentConfigApiError(
                    f"{code}: {message}",
                    http_status=error.response.status_code,
                ) from error

            except httpx.RequestError as error:
                last_error = error
                if retry_safe and attempt < self.max_retries - 1:
                    await asyncio.sleep((2**attempt) + random.uniform(0, 1))
                    continue
                raise

        raise AgentConfigApiError(f"Maximum retries exceeded: {last_error}")

    @staticmethod
    def _unwrap_collection(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("value"), list):
            return payload["value"]
        raise AgentConfigApiError(
            "AgentConfiguration API returned an invalid collection response"
        )

    async def list_agent_configs(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", self._collection_path())
        return self._unwrap_collection(payload)

    async def search_agents(self, search_string: str) -> list[dict[str, Any]]:
        if not isinstance(search_string, str) or not search_string.strip():
            raise ValueError("searchString must be a non-empty string")
        normalized = search_string.strip()
        if len(normalized) > _MAX_SEARCH_LENGTH:
            raise ValueError(
                f"searchString must not exceed {_MAX_SEARCH_LENGTH} characters"
            )
        payload = await self._request(
            "POST",
            f"{self._collection_path()}/SearchAgents",
            json={"SearchString": normalized},
            idempotent=True,
        )
        return self._unwrap_collection(payload)

    async def create_agent_config(self, title_id: str) -> dict[str, Any]:
        title_id = _validate_title_id(title_id)
        return await self._request(
            "POST",
            self._collection_path(),
            json={"TitleId": title_id},
        )

    async def get_agent_config(
        self,
        title_id: str,
        *,
        select_fields: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        params = {"$select": ",".join(select_fields)} if select_fields else None
        return await self._request(
            "GET",
            self._agent_path(title_id),
            params=params,
        )

    async def update_agent_config(
        self,
        title_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ValueError("config must be a JSON object")
        return await self._request(
            "PATCH",
            self._agent_path(title_id),
            json=_to_api_payload(config),
            idempotent=True,
        )

    async def delete_agent_config(self, title_id: str) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            self._agent_path(title_id),
            idempotent=True,
        )

    async def view_agent_icon(self, title_id: str) -> dict[str, Any]:
        return await self.get_agent_config(
            title_id,
            select_fields=("titleId", "name", "icon"),
        )

    async def open_accent_color(self, title_id: str) -> dict[str, Any]:
        return await self.get_agent_config(
            title_id,
            select_fields=("titleId", "branding"),
        )

    async def open_quick_links(self, title_id: str) -> dict[str, Any]:
        return await self.get_agent_config(
            title_id,
            select_fields=("titleId", "quickLinksConfig"),
        )

    async def open_starter_prompts(self, title_id: str) -> dict[str, Any]:
        return await self.get_agent_config(
            title_id,
            select_fields=("titleId", "pivots"),
        )
