# Chat quality: first coordinated pass

This pass improves five parts of Kyrex Chat using actual owner requests as
regressions. It does not establish model parity with another assistant.

| Area | Behavior in this pass |
| --- | --- |
| Overwatcher follow-through | Natural email requests enter the coordinator engine so it can search, read and follow an actual source link. Explicit Gmail commands and numbered selections retain their deterministic routes. |
| Connector setup | Connect reserves the consent window during the tap, then opens the backend-provided URL. Blocked popups get a Continue connecting link. The UI observes actual backend status rather than treating an opened window as success. |
| Conversation clarity | Show one current progress line. Deduplicated history stays in collapsed Activity. Approval requests remain visible. Prompts request brief progress and useful answers first. |
| Complete workflows | Read-only Browser delegations can return verified durable results within a shared 60-second budget of actual waiting, in observations of at most 20 seconds. Status checks can wait for existing reads within the same budget. Slower work keeps the existing asynchronous lifecycle. Browser reads retain bounded page text and actual observed source URLs. |
| Regression evaluation | Replay field-trip email searches, exact subjects, stored numbered selections and compound link requests. Exercise foreign-task rejection, cancellation, approval boundaries and Browser source handling. |

## Evidence and boundaries

The Browser follow-through checks the owner, coordinator, conversation, target
Bot, delegation and task relationship before returning the existing public
result. It does not execute a target inline, widen permissions, approve actions,
or cancel slow background work. Email and page contents remain data, never
instructions authorizing another action.

Dense short schedules reuse the existing target-event window for display only;
fact extraction keeps its original source. This preserves sibling time/location
facts while avoiding an unrelated event in the answer's introductory facts.

The offline suite tests deterministic routing, source handling, task lifecycle,
and UI behavior. It does not measure a live provider's success rate at completing
an entire email-to-form workflow. After deployment, replay that workflow with the
configured provider and a connected Browser Bot, checking the cited page and
whether missing details are honestly reported.

## Deployment and live checks

Deploy both Cloud and Chat for the routing and connector/UI changes. Rebuild the
Browser Host agent to receive the new observed-source projection. Older agents
still return ordinary read results but do not supply the new source metadata.

Messages uses the existing manual Chrome setup; this pass simplifies the hub
presentation, not Google account pairing itself. Live sign-in, emoji pairing,
and subsequent reads still require the owner's PC/phone test. Opening setup is
not recorded as a successful connection.
