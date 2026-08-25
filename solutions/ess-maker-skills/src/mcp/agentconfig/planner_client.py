# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Planner client: the landing-page client core plus the WeveNova planner and
role-attestation endpoint mixins.

``PlannerClient`` reuses everything the landing-page ``AgentConfigClient``
already owns — MSAL/bearer token acquisition, the ``tid`` tenant decode, the
shared httpx session, and the retrying ``_request`` — and layers the beta
project/plan/task and role-attestation surfaces on top through mixins. It adds
only the two pieces those surfaces need beyond the base client: the beta
projects base URL and the caller's ``oid`` (decoded from the same token).
"""

from __future__ import annotations

import os

import httpx

from client import AgentConfigClient
from planner import PlannerMixin
from roles import RolesMixin
from _odata import (
    DEFAULT_AGENTCONFIG_PROJECTS_BASE_URL,
    _decode_object_id_from_jwt,
    _validate_https_base_url,
)


class PlannerClient(PlannerMixin, RolesMixin, AgentConfigClient):
    """Async client for the AgentConfiguration project / plan / task and role
    attestation routes, composed onto the shared landing-page client core."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(transport=transport)
        self.projects_base_url = _validate_https_base_url(
            os.environ.get(
                "AGENTCONFIG_PROJECTS_BASE_URL",
                DEFAULT_AGENTCONFIG_PROJECTS_BASE_URL,
            ),
            "AGENTCONFIG_PROJECTS_BASE_URL",
        )
        self._caller_object_id = _decode_object_id_from_jwt(self._token)

    def __repr__(self) -> str:
        return (
            f"<PlannerClient projects_base_url={self.projects_base_url!r} "
            f"tenant_id={self.tenant_id!r}>"
        )
