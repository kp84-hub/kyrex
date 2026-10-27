# Reusing connected-service handoffs

A coordinator may need several distinct capabilities to answer a request.
It should not start additional Bots merely because an accepted task is pending
or because the model chooses a different Bot name for the same lookup.

The routed email path previously allowed a model to choose an alternate target
after the initial handoff without checking the first task's actual state. After
one connected fallback, routing fell through to generic delegation. The email
lookup cache also included the requested Bot id, allowing the same search to
run again on another persona. These are confirmed source-level paths; a specific
production occurrence still needs its request/task history for attribution.

Connected Gmail and selected-email Calendar handoffs now retain their accepted
task for each authenticated owner, coordinator, parent conversation and user
turn. The host follows queued/running/awaiting-approval work instead of queuing
another copy. A completed email lookup can continue with a new search or numbered
read on the same Bot. The same query/message cache is independent of Bot names
and remains scoped to the current email connection epoch and result page.

One alternate read target is permitted only after a persisted failed read.
Further automatic fallback is stopped, and canceled reads are not restarted.
Calendar handoffs are never automatically replayed, including failed or canceled
tasks and uncertain provider outcomes. Existing exact-event approval remains
authoritative. The host validates owner, coordinator, conversation, turn, Bot,
executor and task/delegation links before returning an existing handoff.

The stable user-turn identity is recorded on coordinator delegations. A new
engine session can recover an accepted handoff from CloudTaskStore after a
transport/process interruption. A genuinely new user turn may perform a fresh
read. No queue, agent loop, capability grant or automatic approval is added.

A typed Browser subtask remains available for distinct source research; it no
longer consumes an initial mail/calendar route simply by happening first.
Developer and fitness task routing, task status observation, and target-specific
permissions remain on their existing paths. This change does not impose a global
one-Bot limit on legitimate multi-step requests.

Offline tests cover pending work, completed evidence, same-Bot continuations,
bounded failed-read fallback, cancellation, calendar non-replay, recovered user
turns, changed/foreign bindings, parallel-turn isolation and auxiliary browsing.
