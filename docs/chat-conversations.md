# Bot conversations

Each owner has one ongoing conversation per Bot ID. Choosing a Bot reopens
that conversation. Header navigation, Bot conversation creation, voice,
onboarding, email rules and delegated Workout/Vision tasks use the same
owner-scoped lookup. A private primary reference pins the first selected
existing thread, so a late update in an older thread cannot redirect the next
handoff. Selection/creation is serialized across shared-volume web and worker
processes, and still checks Bot visibility, lifecycle and account restrictions.

Every specialist return (including Calendar, Email, Browser and Developer)
appends its request and report to that same Bot conversation. Report publication
uses the same cross-process Bot lock. Task-scoped message identities prevent a
reloaded result from appearing twice; distinct tasks with identical report text
still have separate entries. Existing execution, receipt and approval bindings
remain authoritative and are never reassigned or replayed by this change.

The sidebar shows one Bot row, without a History submenu or a separate new-Bot
conversation action. Ordinary **New Chat** still creates an independent general
chat. `POST /api/conversations` with a Bot ID opens its ongoing conversation;
without a Bot ID it continues creating a new ordinary chat. The low-level
record creator remains available for existing isolated internal contexts and
tests, but normal Bot navigation and delegation do not create fresh records.

Existing Bot threads appear in one chronological timeline through the normal
conversation GET response. Their files are retained for compatibility and
rollback; source conversation/message IDs preserve exact draft actions,
images, task controls and old notification links. Ordinary chats, different Bot
IDs, owners and workspace-bound conversations stay separate. The engine's
bounded recent persisted dialogue also includes previous same-Bot threads.
Read projections are never written back as an authoritative transcript or used
to grant approval, replay work, or expand tool permissions. Fitness read-only
execution retains its restricted context.

## Shared follow-up context

Routing and answering use the persisted conversation, including native and
delegated turns that bypassed a model session. Routing receives at most three
prior owner asks, bounded to 5,000 characters; assistant/tool bodies are not
included in that routing projection. Budget-capable Bot routing keeps its
closed context boundary. Request text and history are absent from routing logs.

Every Chat Bot engine receives up to twelve recent user/assistant messages
(12,000 characters, at most 2,500 per message), even when its own system prompt
prevents generic Chat identity injection. A separate `conversationContext`
field refreshes one data block per turn without changing the Bot's identity,
model or tools. Empty context clears old data, and a later headless turn cannot
retain a stale Chat snapshot as its latest reference. Private bank chat and
restricted fitness execution retain their existing separate input boundaries.

The shared conversation policy asks models to interpret natural follow-ups
against the latest relevant ask and result, and to clarify ambiguous references.
It applies to email, calendar, research, coding and ordinary conversation.
An exact bare retry can recover its omitted subject through one shared helper;
native read adapters still validate their operation and current scope. Truncated
routing context cannot become an executable repeat. New topics supersede old
ones. Existing task receipts, uncertain outcomes and approvals remain the
authority for any external action; conversation context cannot grant a replay.
No extra model call is added, although bounded context adds input tokens.

The Overwatcher interprets the owner's reply before delegating. Jev's Bot
recommendation is advisory; the host preserves the coordinator's chosen target
while enforcing existing owner, capability, task and approval checks. A target
task must stand alone, with resolved references, relevant known facts and the
requested result in that Bot's supported format. Casual interest in an alert
does not imply event creation. Calendar chat sends ordinary replies to its
model with the saved dialogue; unsupported Calendar delegations return a
recoverable refusal before any task or target transcript is created. Event
requests and replies to a pending event draft keep their existing validation
and review path.

Deploy web, worker and frontend together and reload Chat. No production
transcripts or task records are deleted or rewritten by deployment.
