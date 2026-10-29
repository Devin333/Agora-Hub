"""The compose file the desktop app runs in local mode, and what it relies on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from interfaces.services.deployment_profile import load_hosted_settings

DESKTOP = Path(__file__).resolve().parents[2] / "deploy" / "desktop"
COMPOSE = DESKTOP / "compose.images.yml"
TEMPLATE = DESKTOP / ".env.template"
VARIABLE = re.compile(r"\$\{([A-Z0-9_]+)(?::?[-?][^}]*)?\}")


@pytest.fixture(scope="module")
def stack() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def template_keys() -> set[str]:
    keys = set()
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            keys.add(line.split("=", 1)[0])
    return keys


def test_every_variable_the_stack_reads_is_in_the_template() -> None:
    used = set(VARIABLE.findall(COMPOSE.read_text(encoding="utf-8")))
    assert used == template_keys()


def test_only_the_web_server_is_published_and_only_on_loopback(stack: dict) -> None:
    services = stack["services"]
    assert set(services) == {"api", "web"}
    assert "ports" not in services["api"]
    assert services["web"]["ports"] == ["127.0.0.1:${AGORA_WEB_PORT}:3000"]


def test_both_services_drop_privileges_and_survive_the_app_quitting(stack: dict) -> None:
    for name, service in stack["services"].items():
        hardening = {**stack["x-hardening"], **service}
        assert hardening["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in hardening["security_opt"], name
        assert hardening["restart"] == "unless-stopped", name
        assert service["mem_limit"], name


def test_the_stack_token_reaches_only_the_web_server(stack: dict) -> None:
    assert "AGORA_DESKTOP_STACK_TOKEN" in stack["services"]["web"]["environment"]
    assert "AGORA_DESKTOP_STACK_TOKEN" not in stack["services"]["api"]["environment"]


def test_the_api_accepts_the_local_stack_settings() -> None:
    # The local stack trusts no proxy and serves plain http on loopback.
    settings = load_hosted_settings(
        {
            "NEWSROOM_DEPLOYMENT_PROFILE": "hosted",
            "NEWSROOM_PUBLIC_ORIGIN": "http://127.0.0.1:45180",
            "NEWSROOM_ALLOW_INSECURE_ORIGIN": "1",
            "NEWSROOM_TRUSTED_PROXY_CIDRS": "",
        }
    )
    assert settings.trusted_proxies == ()
    assert settings.public_origin == "http://127.0.0.1:45180"
    assert settings.secure_cookies is False
