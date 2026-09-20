// api.js — Kyrex Chat backend client.

const BASE = '/api';

async function handle(resp) {
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* not json */
    }
    const err = new Error(detail || `Request failed (${resp.status})`);
    err.status = resp.status; // 401 → the UI offers the same-origin sign-in link
    throw err;
  }
  return resp.json();
}

export async function listConversations() {
  const resp = await fetch(`${BASE}/conversations`);
  const data = await handle(resp);
  return data.conversations || [];
}

// Creates a conversation. With botId, the server validates the Bot binding
// (exists, visible to the user, resolvable Rift) and persists it; without
// one this is ordinary Kyrex Chat, unchanged.
export async function createConversation(botId) {
  const payload = botId ? { bot_id: botId } : {};
  return handle(
    await fetch(`${BASE}/conversations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  );
}

// Bots visible to the authenticated user (id/name/status/model only — the
// backend never exposes rift paths, policies, or credentials).
export async function listBots() {
  const data = await handle(await fetch(`${BASE}/bots`));
  return data.bots || [];
}

// Creates a user-owned Bot. The BASIC flow sends only `name` (+ `role` and a
// `model`); the backend derives the stable id from the name, creates a safe
// Rift server-side, and defaults the provider profile/model from the caller's
// own configured profile. Everything else is advanced and optional: an
// explicit `id`, `providerProfileId` (the backend validates the owner-scoped
// pair and never receives the profile's secret), a `preset` or explicit
// `policy`, a `browserAllowlist` of bare domains, an initial `status`, and a
// `workspaceId` selecting a SERVER-REGISTERED workspace as the Rift. The
// backend never accepts a raw filesystem path, and a Bot created with no
// provider profile stays unconfigured — it fails closed at turn time rather
// than inheriting any global default.
export async function createBot({
  id, name, role, model,
  providerProfileId, systemPrompt, preset, policy,
  browserAllowlist, status, workspaceId,
}) {
  // Omit an absent id/model so the server can generate/default it.
  const payload = { name };
  if (id) payload.id = id;
  if (role) payload.role = role;
  if (model) payload.model = model;
  if (providerProfileId) payload.provider_profile_id = providerProfileId;
  if (systemPrompt) payload.system_prompt = systemPrompt;
  if (preset) payload.preset = preset;
  if (policy) payload.policy = policy;
  if (Array.isArray(browserAllowlist) && browserAllowlist.length) {
    payload.browser_allowlist = browserAllowlist;
  }
  if (status) payload.status = status;
  if (workspaceId) payload.workspace_id = workspaceId;
  return handle(
    await fetch(`${BASE}/bots`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  );
}

// Named Bot configuration presets (id/label/policy + the host-derived
// effective permissions) plus the PRIMARY capability options for the one
// Change capability control (deterministic server-side labels/descriptions).
export async function listBotPresets() {
  return handle(await fetch(`${BASE}/bots/presets`));
}

// Explicitly configure a user-owned Bot. `payload` may carry a named
// `preset` (e.g. "developer"), an explicit `policy`, a `system_prompt`, or a
// `model`. The server is owner-scoped and fails closed when a writable Bot's
// Rift is not a real repository.
export async function configureBot(botId, payload) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}/configure`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  );
}

// One-time claim of an OWNERLESS legacy Bot. The server only allows the
// configured Kyrex web operator to claim, and only when the Bot has no owner —
// a Bot owned by anyone else (409) is never overwritten. Claiming records the
// caller as owner and nothing else: it does not start the Bot or change its
// policy. After a successful claim the Bot becomes owner-manageable, so the
// existing Start/Pause/Stop and Configure controls apply.
export async function claimBot(botId) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}/claim`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
  );
}

// Owner-scoped Bot lifecycle update. `status` is "running", "paused", or
// "stopped". "running" makes the Bot eligible for NEW Chat conversations and
// task submissions; "paused"/"stopped" reject new work. This toggles a work-
// eligibility LABEL only — Kyrex runs Bots on a shared worker, so no separate
// process is started or stopped. The server is owner-scoped (403 otherwise).
export async function updateBotStatus(botId, status) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    })
  );
}

// Owner-scoped browser domain allowlist update, via the SAME PATCH route the
// lifecycle update uses. Bare hostnames only; the server re-validates every
// entry and fails closed (an empty list denies every navigation). The
// response is the redacted Bot view, including the new allowlist.
export async function updateBotAllowlist(botId, browserAllowlist) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ browser_allowlist: browserAllowlist || [] }),
    })
  );
}

// The Bot's bound Browser Host plus the owner's ELIGIBLE hosts to choose from.
// The backend is owner-scoped and returns only redacted host views (never a
// secret, a sealed blob, or a CDP URL). Availability is same-owner Bots only.
export async function getBotBrowserHost(botId) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}/browser-host`)
  );
}

// Explicitly bind the Bot to ONE of the owner's hosts. The server refuses an
// unknown, revoked, or foreign-owned host (409) and never guesses a host
// implicitly — a bound Bot runs only on the host chosen here.
export async function bindBotBrowserHost(botId, hostId) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}/browser-host`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host_id: hostId }),
    })
  );
}

// Remove the Bot's host binding (idempotent, owner-scoped).
export async function unbindBotBrowserHost(botId) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}/browser-host`, {
      method: 'DELETE',
    })
  );
}

// Owner-scoped Change capability control — the ONE user-facing control that
// replaces the wall of Configure-as actions. `capability` is one of the
// server's PRIMARY capability ids ('chief-of-staff' | 'calendar' |
// 'developer' | 'browser'). The server derives the policy, the bot name, and
// the description deterministically from its own capability table (truthful
// Chief of Staff reporting), and applies the same fail-closed surface gates
// as the configure endpoint.
export async function changeBotCapability(botId, capability) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}/capability`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ capability }),
    })
  );
}

// Owner-scoped delete with EXPLICIT name confirmation. The caller must echo
// the bot's exact current name; the server stops the bot and removes its
// registry/configuration entry while preserving conversations, task history,
// Google authorization, provider profiles, and calendar events.
export async function deleteBot(botId, confirmName) {
  return handle(
    await fetch(`${BASE}/bots/${encodeURIComponent(botId)}/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm_name: confirmName }),
    })
  );
}

// Detect the owner's legacy calendar-family Bots (Calendar Reader, Calendar
// Writer, Glofox Reader, Level 6, Level 6 Calendar) so the UI can offer the
// safe consolidation into ONE Calendar Bot. Read-only.
export async function fetchBotMigration() {
  const data = await handle(await fetch(`${BASE}/bots/migrate`));
  return data;
}

// Consolidate the owner's legacy calendar-family Bots into ONE Calendar Bot.
// The server NEVER deletes a legacy bot — it stops each one and records
// `migrated_to` on the record — and reports exactly what stopped/moved.
export async function migrateLegacyCalendarBots() {
  const data = await handle(
    await fetch(`${BASE}/bots/migrate/legacy-calendar`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
  );
  return data;
}

// The connected Google account (email) + destination calendar id (non-secret,
// read-only).
export async function fetchGoogleAccount() {
  const data = await handle(await fetch(`${BASE}/connections/google/account`));
  return data;
}

// The owner's destination-calendar choices (calendarList, read-only).
export async function fetchGoogleCalendars() {
  const data = await handle(await fetch(`${BASE}/connections/google/calendars`));
  return data;
}

// Set the owner's destination calendar (Change calendar control).
export async function setGoogleCalendar(calendarId) {
  return handle(
    await fetch(`${BASE}/connections/google/calendar`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ calendar_id: calendarId }),
    })
  );
}

export async function getConversation(conversationId) {
  return handle(await fetch(`${BASE}/conversations/${conversationId}`));
}

export async function deleteConversation(conversationId) {
  return handle(
    await fetch(`${BASE}/conversations/${conversationId}`, { method: 'DELETE' })
  );
}

export async function listChatProviders() {
  const data = await handle(await fetch(BASE + '/chat/providers'));
  return data.providers || [];
}

export async function listProviderProfiles() {
  const data = await handle(await fetch(BASE + '/chat/provider-profiles'));
  return data.profiles || [];
}

export async function saveProviderProfile(profile) {
  return handle(await fetch(BASE + '/chat/provider-profiles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(profile),
  }));
}

export async function deleteProviderProfile(profileId) {
  return handle(await fetch(BASE + '/chat/provider-profiles/' + encodeURIComponent(profileId), {
    method: 'DELETE',
  }));
}

export async function updateConversationSettings(conversationId, provider, model) {
  return handle(await fetch(BASE + '/conversations/' + encodeURIComponent(conversationId) + '/settings', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, model }),
  }));
}

export async function chatStatus() {
  return handle(await fetch(`${BASE}/chat/status`));
}

// Server-registered workspaces attachable to a conversation. Only ids and
// names are returned by the backend — never filesystem paths — so the client
// can only ever reference a server-controlled registry entry.
export async function listWorkspaces() {
  const data = await handle(await fetch(`${BASE}/chat/workspaces`));
  return data.workspaces || [];
}

// Attach (or, with workspaceId=null, detach) a registered workspace on an
// existing conversation. The server validates the id against its registry.
export async function attachWorkspace(conversationId, workspaceId) {
  return handle(
    await fetch(`${BASE}/chat/workspace`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: conversationId,
        workspace_id: workspaceId || null,
      }),
    })
  );
}

// Generates a client-side request_id so /api/chat/cancel can target the
// exact in-flight generation (the backend keys its cancel registry on it).
export function newRequestId() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Records an approval reply against a specific Bot task. The backend scopes
// the reply to the task's pending approval (task_id + chat ownership), so a
// reply can never resolve a different Bot's or conversation's approval.
export async function respondTask(taskId, text) {
  return handle(
    await fetch(`${BASE}/task/${encodeURIComponent(taskId)}/respond`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
  );
}

// Owner-scoped view of delegated (Bot-to-Bot) work. Returns safe public views
// only: coordinator/target ids, status, timestamps, the task text, and a
// sanitized final summary. Never provider keys, Rift paths, prompts, approval
// secrets, or browser-session metadata. This is status-only — there are
// deliberately no approve/cancel controls here; a delegated approval is
// answered only by the owner through the target task's existing flow.
//
// With a conversationId this is the Delegated Work card's refresh path: the
// backend reconciles each delegation against its target task and, when work
// just finished, relays the result into the conversation exactly once. The
// response is `{ delegations, relayed }` — `relayed` carries any terminal
// notices produced by THIS call so the open transcript can append them live.
export async function fetchDelegations(conversationId) {
  const q = conversationId
    ? `?conversation_id=${encodeURIComponent(conversationId)}`
    : '';
  const data = await handle(await fetch(`${BASE}/delegations${q}`));
  return {
    delegations: data.delegations || [],
    relayed: data.relayed || [],
  };
}

// Cancels an in-flight generation server-side (idempotent when unknown).
export async function cancelChat(requestId) {
  return handle(
    await fetch(`${BASE}/chat/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: requestId }),
    })
  );
}

// Opens an SSE stream for a chat turn. Returns an object with `cancel()`,
// an async iterator-compatible `stream` of parsed events, and the
// `requestId` that was sent so Stop can cancel this exact stream.
//
// `workspaceId` (optional): a server-registered workspace id to use for this
// turn. Omitted → the conversation keeps its stored binding (pure chat when
// none). The value is only ever a registry id — never a filesystem path.
export function streamChat(conversationId, message, requestId, workspaceId) {
  const controller = new AbortController();
  const reqId = requestId || newRequestId();

  const payload = {
    conversation_id: conversationId || '',
    message,
    request_id: reqId,
  };
  if (workspaceId !== undefined && workspaceId !== null && workspaceId !== '') {
    payload.workspace_id = workspaceId;
  }

  async function* stream() {
    const resp = await fetch(`${BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = await resp.json();
        if (body && body.detail) detail = body.detail;
      } catch {
        /* not json */
      }
      const err = new Error(detail || `Request failed (${resp.status})`);
      err.status = resp.status; // 401 → the UI offers the same-origin sign-in link
      throw err;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (!frame.trim()) continue;
        for (const line of frame.split('\n')) {
          if (line.startsWith('data:')) {
            const payload = line.slice(5).trim();
            if (payload) yield JSON.parse(payload);
          }
        }
      }
    }
  }

  // Return the started iterator (not the generator function): consumers do
  // `for await (const event of stream)` on it exactly once.
  return { stream: stream(), cancel: () => controller.abort(), requestId: reqId };
}


// ── Connections (Google Calendar, read-only) ────────────────────────────
//
// Every call hits the owner-authenticated backend routes (connections_api.py).
// The backend re-validates EVERYTHING fail closed — owner auth on every method,
// the single-use owner-bound redirect-bound OAuth state, and the idempotent
// disconnect. NO method here receives, returns, or renders a token, client
// secret, or authorization code: connect returns an authorization URL only.
export async function fetchConnections() {
  return handle(await fetch(`${BASE}/connections`));
}

export async function connectGoogle() {
  return handle(
    await fetch(`${BASE}/connections/google/connect`, { method: "POST" })
  );
}

export async function upgradeGoogleCalendarWrite() {
  return handle(
    await fetch(`${BASE}/connections/google/upgrade-write`, { method: "POST" })
  );
}

export async function disconnectGoogle() {
  return handle(
    await fetch(`${BASE}/connections/google/disconnect`, { method: "POST" })
  );
}
