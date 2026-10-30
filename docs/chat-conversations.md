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

Deploy web, worker and frontend together and reload Chat. No production
transcripts or task records are deleted or rewritten by deployment.
