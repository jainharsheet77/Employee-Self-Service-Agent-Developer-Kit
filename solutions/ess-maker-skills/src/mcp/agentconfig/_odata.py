# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Shared OData helpers for the WeveNova AgentConfiguration beta surface.

These helpers are used by both the planner (projects/plans/tasks) and the role
attestation endpoint modules. They intentionally have no dependency on the
client class, so the domain mixins can import them without creating an import
cycle with ``client``.
"""

from __future__ import annotations

import base64
import binascii
import json
import urllib.parse
from typing import Any, Optional


DEFAULT_AGENTCONFIG_PROJECTS_BASE_URL = "https://substrate.office.com/weveb2/api/beta"
_TENANTS_COLLECTION = "tenants"

_QUERY_OPTION_MAP = {
    "select": "$select",
    "expand": "$expand",
    "filter": "$filter",
    "orderby": "$orderby",
    "top": "$top",
    "skip": "$skip",
    "count": "$count",
    "skiptoken": "$skiptoken",
}


def _decode_object_id_from_jwt(token: str) -> Optional[str]:
    """Best-effort decode of the caller's Entra object id (``oid`` claim).

    Used to scope "tasks for the caller" queries to the signed-in principal
    without taking the identity as a tool argument. Returns ``None`` when the
    token is opaque or carries no ``oid`` claim.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return None
    object_id = payload.get("oid")
    if isinstance(object_id, str) and object_id:
        return object_id
    return None


def _validate_https_base_url(url: str, env_name: str) -> str:
    """Validate an HTTPS base URL without credentials, query, or fragment."""
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{env_name} must be an HTTPS URL without credentials, a query, "
            "or a fragment"
        )
    return url.rstrip("/")


def _require_odata_id(value: str, name: str) -> str:
    """Validate a non-empty, control-char-free id and encode it as an OData key."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            f"{name} must be a non-empty string without surrounding whitespace"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return urllib.parse.quote(value.replace("'", "''"), safe="")


def _escape_odata_literal(value: str, name: str) -> str:
    """Validate and single-quote-escape a value for an OData ``$filter`` literal."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            f"{name} must be a non-empty string without surrounding whitespace"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value.replace("'", "''")


def _mutation_headers(
    etag: Optional[str] = None, idempotency_key: Optional[str] = None
) -> dict[str, str]:
    """Build optional If-Match / Idempotency-Key headers for a mutation."""
    headers: dict[str, str] = {}
    if etag is not None:
        if not isinstance(etag, str) or not etag.strip():
            raise ValueError("etag must be a non-empty string when provided")
        headers["If-Match"] = etag
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError(
                "idempotencyKey must be a non-empty string when provided"
            )
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _normalize_etag(value: Optional[str]) -> Optional[str]:
    """Normalize an ETag for equality comparison only.

    Strips a weak-validator ``W/`` prefix and surrounding quotes so that the
    same version rendered as ``W/"3"``, ``"3"``, or ``3`` compares equal. This
    is used to decide whether a re-read entity's ETag actually moved; it is not
    the value sent back on the wire (the caller's original ETag string is).
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if trimmed[:2].lower() == "w/":
        trimmed = trimmed[2:].strip()
    return trimmed.strip('"')


def _entity_scalar(entity: Any, *names: str) -> Optional[str]:
    """Case-insensitively read the first present non-empty string field.

    WeveNova renders entity bodies in PascalCase (``ETag``, ``Status``) while
    OData also permits the ``@odata.etag`` annotation; callers pass every
    accepted spelling and get back the first non-empty match.
    """
    if not isinstance(entity, dict):
        return None
    lowered = {
        key.lower(): item for key, item in entity.items() if isinstance(key, str)
    }
    for name in names:
        item = lowered.get(name.lower())
        if isinstance(item, str) and item:
            return item
    return None


def _build_query_params(query: Optional[dict[str, Any]]) -> dict[str, str]:
    """Map friendly OData option names (filter/top/...) to ``$``-prefixed params."""
    if query is None:
        return {}
    if not isinstance(query, dict):
        raise ValueError("query must be an object of OData options")
    params: dict[str, str] = {}
    for key, value in query.items():
        if value is None:
            continue
        option = _QUERY_OPTION_MAP.get(key.lower())
        if option is None:
            raise ValueError(f"Unsupported query option: {key}")
        params[option] = (
            "true" if value is True else "false" if value is False else str(value)
        )
    return params
