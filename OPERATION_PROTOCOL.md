# Operation Protocol — Announce Intent, Not Approvals

Status: draft. Extends the executor contract in `K_BOT_DESIGN.md`. Prerequisite
for step 6 of `K_BOT_AUTONOMY.md` (policy enforcement).

## The problem

`audit.jsonl` today records approval decisions, because `audit.log()` is called
inside the `KYREX_APPROVAL:` handler. That means it answers:

> What did we ask the operator to approve?

Step 6 needs a different question answered:

> What would this Bot have done if it were autonomous?

Those are different datasets, and the difference is not small. The executor
decides what to raise an approval for, so anything it handles silently — every
tier-0 read, every operation a permissive policy would wave through — never
reaches the host and never appears in the log. Under the policy most Bots would
run, that is the majority of what they do.

Designing enforcement from approval history means designing it from the subset
of operations the executor already thought were worth stopping for. It would
tell you nothing about the 90% you are trying to decide whether to
pre-authorize.

## The change

The executor announces **intent** for every operation, before performing it.
The host decides what happens.

```
Executor                          Host
   |
   |  KYREX_OPERATION: {...}
   |------------------------------>
   |                               resolve ExecutionContext
   |                               derive tier from the operation
   |                               evaluate policy
   |                               AUDIT
   |                               |
   |         ALLOW                 +--> tier 0, or policy pre-authorized
   |<------------------------------
   |  (executor proceeds)
   |
   |         DENY                  +--> policy denied
   |<------------------------------
   |  (executor must not proceed)
   |
   |         APPROVE               +--> needs a human
   |<------------------------------
   |  KYREX_APPROVAL: {...}
   |------------------------------>
   |                               prompt operator, await reply
   |         APPROVED / DENIED
   |<------------------------------
```

Ordering is the point: **intent → policy → audit → authorization → execution.**
Not execution → approval handler → audit. A denied operation is recorded
because the record is written before the decision is acted on, not after.

## The line

```
KYREX_OPERATION:{"op":"fs.write","target":"notes.md","summary":"write notes.md (42 bytes)","detail":"..."}
```

- `op` — a dotted operation name, executor-namespaced: `fs.read`, `fs.write`,
  `fs.delete`, `repo.commit`, `repo.push`.
- `target` — what it acts on, for the audit record and the policy match.
- `summary` — one line, shown to the operator if this becomes an approval.
- `detail` — optional, longer context for the approval prompt.

**There is no `tier` field, and this is deliberate.** An executor that could
declare its own tier could declare a delete as tier 0 and be waved through.
The host derives the tier from `op` and the Bot's policy. The executor
announces what it wants to do; it does not get an opinion on how dangerous
that is.

The host replies with exactly one line on the executor's stdin:

- `ALLOW` — proceed without further ceremony.
- `DENY` — do not proceed. The executor must treat this as final and must not
  retry the same operation by another route.
- `APPROVE` — a human is needed. The executor then emits its
  `KYREX_APPROVAL:` line and blocks as it does today.

## Compatibility

Executors that never emit `KYREX_OPERATION:` behave exactly as they do now:
`KYREX_APPROVAL:` still works, still blocks, still prompts. The new line is
additive, so `git_workflow.py` needs no changes to keep working while `fs` is
migrated.

The host treats an unrecognised `op` as deny, not as allow. An operation the
policy has no rule for is already default-deny; an operation the *host* does
not recognise is a stronger signal that something is wrong.

## Audit record

Every operation produces one entry, whatever the outcome:

```json
{"timestamp":"...","bot_id":"cloudbot","session_id":"cloudbot",
 "op":"fs.write","target":"notes.md","tier":1,
 "policy_rule":"fs:write","decision":"approval_required",
 "outcome":"approved"}
```

- `decision` — what the host decided from policy: `allow`, `deny`, or
  `approval_required`.
- `outcome` — what actually happened: `auto`, `approved`, `denied`, `timeout`,
  or `blocked` for a policy denial.

Splitting these two is what makes the dataset useful. `decision` is what the
policy said; `outcome` is what the human did. Comparing them across a week
answers the only question step 6 needs: *how often would enforcing this policy
have changed anything, and would those changes have been right?*

## Tests this needs

The protocol is a security boundary, so the tests are the specification:

- A tier-0 operation is audited and executed with no approval prompt.
- A tier-1/2 operation is audited **before** the approval is raised.
- A policy-denied operation is audited and never executed.
- A malformed or unknown `op` is denied, not allowed.
- An executor that includes a `tier` field has it ignored — the host's derived
  tier is what applies.
- An unbound session preserves today's behaviour.
- An executor that ignores `DENY` and proceeds anyway is a bug in that
  executor, but the host must not depend on the executor's cooperation for
  anything it can enforce itself.

That last point deserves care. `DENY` is advisory in the sense that a
misbehaving executor can ignore it. Where the host can enforce rather than
ask — filesystem roots via `KYREX_FS_ROOT`, credentials via scoped
capabilities — it should, and the protocol is the audit trail rather than the
only line of defence.

## Build order

1. Host: parse `KYREX_OPERATION:`, derive tier, evaluate policy, audit, reply
   `ALLOW` / `DENY` / `APPROVE`. No executor changes yet — nothing emits the
   line, so nothing changes.
2. `fs_executor.py`: emit the line before every read, write, and delete, and
   honour the reply. Its existing approval flow moves behind `APPROVE`.
3. Verify with a real workload that the audit now records tier-0 reads.
4. Only then: collect several days of data, and decide enforcement from it.
