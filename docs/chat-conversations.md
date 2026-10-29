# Bot conversations

Choosing a Bot in the Chat or Kbot header opens its latest saved conversation.
It creates a thread only if that owner has no eligible conversation for the
selected Bot. The authenticated host rechecks Bot visibility, lifecycle and
workspace availability. A shared filesystem lock serializes selection/creation
across web and worker processes. Opening the Bot does not submit a task or
approve, retry or replay existing work.

The sidebar shows one main row per Bot ID. **History** exposes its other
conversations, including existing duplicate-looking rows. The current thread
stays selected even when it is older. Activity counts include older threads;
task subscriptions still follow the complete conversation list. Ordinary chats
remain separate, and different Bot IDs are never grouped by display name.

History is presentation only. No transcripts, task bindings, approval IDs,
image references or links are merged, reassigned or deleted. Direct task and
notification links keep opening their original conversation. Specialized image
and fitness tasks retain their existing isolated thread behavior while sharing
the Bot's sidebar group.

**New conversation with [Bot]** inside History explicitly creates an independent
Bot conversation. The main **New Chat** button still creates ordinary Kyrex Chat.
Existing `POST /api/conversations` behavior remains unchanged; header navigation
uses authenticated `POST /api/bots/{bot_id}/conversation` instead.
