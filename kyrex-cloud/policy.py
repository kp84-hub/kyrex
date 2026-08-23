"""Bot policy evaluator.

A Bot's policy is a dict stored in the registry, mapping an operation pattern
to a tier:

  - Keys: exact operation strings (e.g. ``"fs:read"``), prefix wildcards
    (e.g. ``"fs:*"``), or the bare ``"*"`` catch-all.
  - Values: ``0``, ``1``, ``2`` (numeric tiers), or the string ``"deny"``.

Matching is most-specific first: exact key → prefix wildcard → ``*``.

Rules
-----
* A ``deny`` rule always denies regardless of the derived tier.
* A numeric rule may only *raise* the effective tier above the derived tier
  (i.e. it cannot grant more trust than the host computed).  The effective
  tier is ``max(policy_value, derived_tier)``.
* If no rule matches, the default is deny.

MODE
----
``MODE`` (module-level variable, default ``"dry_run"``) controls whether
:func:`enforce` actually applies the decision or just reports it.

``dry_run``
    :func:`enforce` returns the *derived_tier* unchanged — the policy is
    evaluated for visibility but behaviour does not change.
``enforce``
    :func:`enforce` returns the *effective_tier* from the decision.
"""

MODE: str = "enforce"  # "dry_run" | "enforce"


# ── Public API ─────────────────────────────────────────────────────────

def evaluate(
    policy: dict[str, str | int],
    operation: str,
    derived_tier: int,
) -> dict:
    """Evaluate *operation* against *policy*.

    Args:
        policy:       The policy dict from a Bot's registry entry.
        operation:    The operation to check (e.g. ``"fs:read"``).
        derived_tier: The tier the host computed for this operation (0, 1,
                      or 2).

    Returns a decision dict with:

    * ``effective_tier`` — ``0``, ``1``, ``2``, or ``"deny"``.
    * ``matched_rule`` — the policy key that matched, or ``None``.
    * ``reason`` — human-readable explanation.
    """
    matched_rule = _match(policy, operation)

    if matched_rule is None:
        return {
            "effective_tier": "deny",
            "matched_rule": None,
            "derived_tier": derived_tier,
            "reason": (
                f"no matching rule for {operation!r} in policy; default deny"
            ),
        }

    policy_value = policy[matched_rule]

    if policy_value == "deny":
        return {
            "effective_tier": "deny",
            "matched_rule": matched_rule,
            "derived_tier": derived_tier,
            "reason": (
                f"rule {matched_rule!r} explicitly denies {operation!r}"
            ),
        }

    # Numeric rule: policy grants autonomy but cannot lower below derived.
    effective_tier = max(int(policy_value), derived_tier)
    return {
        "effective_tier": effective_tier,
        "matched_rule": matched_rule,
        "derived_tier": derived_tier,
        "reason": (
            f"rule {matched_rule!r} grants tier {policy_value}; "
            f"effective tier = max({policy_value}, {derived_tier}) = "
            f"{effective_tier}"
        ),
    }


def enforce(decision: dict) -> int | str:
    """Apply *decision* according to the current :data:`MODE`.

    In ``dry_run`` mode the *derived_tier* (not the effective tier) is
    returned so that the caller's behaviour is unchanged.

    In ``enforce`` mode the *effective_tier* from the decision is returned.

    Args:
        decision: A decision dict returned by :func:`evaluate`.

    Returns:
        A tier (``0``, ``1``, ``2``) or the string ``"deny"``.
    """
    if MODE == "dry_run":
        # In dry-run the caller does not have the derived_tier in hand, so
        # the decision dict carries it implicitly.  We return the numeric
        # derived_tier even when the decision says "deny".
        return decision.get("derived_tier", 0)
    return decision["effective_tier"]


# ── Internal helpers ───────────────────────────────────────────────────

def _match(policy: dict, operation: str) -> str | None:
    """Find the most specific matching key in *policy* for *operation*.

    Returns the policy key or ``None``.
    """
    # 1. Exact match.
    if operation in policy:
        return operation

    # 2. Prefix wildcard — key ends with ":*" and operation has that prefix.
    for key in policy:
        if key.endswith(":*"):
            prefix = key[:-2]  # strip the ":*" suffix
            if operation.startswith(prefix):
                return key

    # 3. Catch-all.
    if "*" in policy:
        return "*"

    return None