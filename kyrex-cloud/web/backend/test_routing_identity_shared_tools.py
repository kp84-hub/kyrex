"""Jev routing identity must not inherit stale per-Bot permission presets."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import routing_identity  # noqa: E402
import serve  # noqa: E402


def test_email_name_beats_stale_calendar_policy_for_routing():
    # This mirrors the live UI state that exposed the architecture bug: the Bot
    # is named Email Bot but an old capability selection left Calendar policy
    # on its registry record. Routing identity must be EMAIL.
    bot = {
        "id": "email-bot",
        "name": "Email Bot",
        "owner": "alice",
        "status": "running",
        "policy": serve.calendar_preset_policy(),
    }
    assert routing_identity._routing_role(serve, bot) == "email"


def test_coordinator_remains_coordinator_routing_role():
    bot = {
        "id": "chief",
        "name": "Chief of Staff",
        "policy": serve.coordinator_preset_policy(),
    }
    assert routing_identity._routing_role(serve, bot) == "chief-of-staff"


def test_install_strips_legacy_policy_capabilities_from_jev_candidates(monkeypatch):
    delegation = SimpleNamespace(
        capability_labels=lambda policy: ["read calendar"],
        role_label=lambda bot: "calendar",
    )
    jev = SimpleNamespace(
        _role_view=lambda policy: {"id": "calendar"},
        _shared_tools=lambda dev_bot, bot: ["gmail_read", "calendar_write"],
    )
    fake_dev = SimpleNamespace(
        calendar_route_ready=lambda bot: True,
        calendar_editor_route_ready=lambda bot: True,
    )

    # Isolate the module-level idempotence flag for this unit test.
    monkeypatch.setattr(routing_identity, "_installed", False)
    routing_identity.install(jev, delegation, serve)

    assert delegation.capability_labels(serve.calendar_preset_policy()) == []
    assert delegation.role_label({"id": "email-bot", "name": "Email Bot"}) == "email"
    assert jev._role_view(serve.calendar_preset_policy()) == {}
    tools = jev._shared_tools(fake_dev, {"id": "email-bot"})
    assert tools == [
        "gmail_read", "calendar_write", "calendar_read", "calendar_delete"]
