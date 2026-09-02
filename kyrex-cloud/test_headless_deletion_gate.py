"""Headless agent deletion approval gate.

No human is present in a headless run, so deletion confirmations must fail
closed: they are answered with an explicit DENY (never approved), which makes
the engine report the deletion as cancelled instead of executing it.
"""

from headless_agent import auto_approve_gate


def test_auto_approve_gate_denies_deletions():
    assert auto_approve_gate("deletion") is False


def test_auto_approve_gate_approves_other_gates():
    assert auto_approve_gate("") is True
    assert auto_approve_gate("confirm") is True
    assert auto_approve_gate("edit") is True