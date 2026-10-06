# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for the neutral AgentConfiguration client core (auth + token cache).

The token-flow behaviour — the shared MSAL cache location and the interactive
form-post sign-in — lives on the neutral ``AgentConfigBaseClient`` core under ``agentconfig_core/`` and
is shared by every AgentConfiguration MCP (landing-page config and planner), so it is
pinned here against ``base_client`` directly rather than any one server's client.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
CORE_DIR = (
    REPO_ROOT
    / "solutions"
    / "ess-maker-skills"
    / "src"
    / "mcp"
    / "agentconfig_core"
)
sys.path.insert(0, str(CORE_DIR))

import base_client  # noqa: E402


def test_token_cache_uses_shared_local_state() -> None:
    assert Path(base_client._TOKEN_CACHE_PATH) == (
        REPO_ROOT
        / "solutions"
        / "ess-maker-skills"
        / "src"
        / "mcp"
        / "agentconfig_core"
        / ".local"
        / "msal_token_cache.bin"
    )


def test_interactive_auth_always_prompts_for_account_selection(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeServer:
        server_port = 12345

        def handle_request(self) -> None:
            base_client._FormPostCaptureHandler.captured = {
                "code": "authorization-code"
            }

        def server_close(self) -> None:
            pass

    class FakeApp:
        def initiate_auth_code_flow(self, **kwargs):
            captured.update(kwargs)
            return {"auth_uri": "https://login.example.test"}

        def acquire_token_by_auth_code_flow(self, flow, response):
            return {"access_token": "token"}

    monkeypatch.setattr(
        base_client.http.server,
        "HTTPServer",
        lambda *args: FakeServer(),
    )
    monkeypatch.setattr(base_client.webbrowser, "open", lambda url: True)

    result = base_client._acquire_token_interactive_form_post(FakeApp())

    assert result == {"access_token": "token"}
    assert captured["prompt"] == "select_account"


def test_refreshed_token_forces_silent_refresh_before_interactive(monkeypatch) -> None:
    # After a 401, the silent attempt must bypass the cached (now server-rejected)
    # access token via force_refresh=True; when the refresh token is gone it
    # escalates to the interactive sign-in.
    import sys
    import types

    class FakeApp:
        def __init__(self) -> None:
            self.silent: dict[str, object] | None = None

        def get_accounts(self) -> list[str]:
            return ["account"]

        def acquire_token_silent(self, scopes, account=None, force_refresh=False):
            self.silent = {"account": account, "force_refresh": force_refresh}
            return None  # refresh token gone -> fall through to interactive

    fake_app = FakeApp()
    monkeypatch.setitem(
        sys.modules,
        "msal",
        types.SimpleNamespace(PublicClientApplication=lambda *a, **k: fake_app),
    )
    monkeypatch.setattr(base_client, "_load_msal_cache", lambda: object())
    monkeypatch.setattr(base_client, "_save_msal_cache", lambda cache: None)
    monkeypatch.setattr(
        base_client,
        "_acquire_token_interactive_form_post",
        lambda app: {"access_token": "interactive-token"},
    )

    token = base_client.acquire_token_msal_refreshed()

    assert token == "interactive-token"
    assert fake_app.silent == {"account": "account", "force_refresh": True}


def test_refreshed_token_force_interactive_skips_the_cache(monkeypatch) -> None:
    # The explicit login path never consults the silent cache — it goes straight to
    # the interactive sign-in the user is asked for.
    import sys
    import types

    class FakeApp:
        def get_accounts(self):
            raise AssertionError("force_interactive must not consult the token cache")

    monkeypatch.setitem(
        sys.modules,
        "msal",
        types.SimpleNamespace(PublicClientApplication=lambda *a, **k: FakeApp()),
    )
    monkeypatch.setattr(base_client, "_load_msal_cache", lambda: object())
    monkeypatch.setattr(base_client, "_save_msal_cache", lambda cache: None)
    monkeypatch.setattr(
        base_client,
        "_acquire_token_interactive_form_post",
        lambda app: {"access_token": "explicit-login"},
    )

    assert (
        base_client.acquire_token_msal_refreshed(force_interactive=True)
        == "explicit-login"
    )


def test_resolve_token_reports_source(monkeypatch) -> None:
    # The source gates whether a 401 can renew in-process: env/file tokens cannot.
    monkeypatch.setenv("AGENTCONFIG_ACCESS_TOKEN", "env-token")
    monkeypatch.delenv("AGENTCONFIG_ACCESS_TOKEN_FILE", raising=False)
    assert base_client._resolve_token_with_source() == ("env-token", "env")

    monkeypatch.setattr(
        base_client, "acquire_token_msal_interactive", lambda: "msal-token"
    )
    monkeypatch.delenv("AGENTCONFIG_ACCESS_TOKEN", raising=False)
    assert base_client._resolve_token_with_source() == ("msal-token", "msal")
