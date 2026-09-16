import React, { useEffect, useState } from 'react';
import {
  bindBotBrowserHost, claimBot, configureBot, createBot, getBotBrowserHost,
  listBotPresets, listProviderProfiles, listWorkspaces, unbindBotBrowserHost,
  updateBotAllowlist, updateBotStatus,
} from '../lib/api.js';
import {
  BROWSER_BOT_BADGE_LABEL, browserBotBadge, browserBotBlockers,
  browserBotPermissionRows, canEnableBrowserBot,
} from '../lib/browserBot.js';

// A Bot's status is a work-eligibility label on the shared Kyrex worker — it
// is never a separate process. "running" admits new Chat conversations/tasks;
// "paused" and "stopped" reject new work (already-running work is not
// interrupted).
//
// High-level state shown in the roster and right after creation: "Ready" when
// the Bot admits new work (running), otherwise "Stopped" — the clear
// Ready/Stopped state a freshly created Bot is shown in.
const STATE_LABEL = {
  running: 'Ready',
  paused: 'Stopped',
  stopped: 'Stopped',
};

function stateLabel(status) {
  return STATE_LABEL[status] || 'Stopped';
}

// The default provider profile + model for a new Bot: the user's OWN configured
// profiles, lowest id first. Returns null when none are configured — the form
// then sends no default and the server fails closed (never a global provider
// fallback).
function defaultSelection(profiles) {
  if (!profiles || profiles.length === 0) return null;
  const sorted = [...profiles].sort((a, b) =>
    String(a.id || '').localeCompare(String(b.id || '')));
  const first = sorted[0];
  const models = first.models || [];
  if (models.length === 0) return null;
  return { provider_profile_id: first.id, model: models[0] };
}

function profileLabel(profiles, profileId) {
  const p = (profiles || []).find((x) => x.id === profileId);
  return p ? (p.name || p.id) : 'your provider profile';
}

function lifecycleMessage(bot, status) {
  const name = bot.name || bot.id;
  if (status === 'running') {
    return `${name} is running — eligible for new Chat conversations and task work. `
      + 'Kyrex runs Bots on a shared worker, so no separate process was started.';
  }
  if (status === 'paused') {
    return `${name} is paused — new turns and tasks are rejected. `
      + 'Work already accepted keeps running to completion.';
  }
  return `${name} is stopped — new turns and tasks are rejected. `
    + 'Work already accepted keeps running to completion.';
}

// Human-readable label + CSS class for a host-derived effective tier. Values
// come straight from the backend (serve.effective_permissions): 0 = host
// auto-allows, 1/2 = the EXISTING approval flow decides, "deny" = blocked.
function permissionView(tier) {
  if (tier === 0) return { text: 'allowed', cls: 'perm-allow' };
  if (tier === 1 || tier === 2) {
    return { text: `approval required (T${tier})`, cls: 'perm-approve' };
  }
  return { text: 'denied', cls: 'perm-deny' };
}

// The blank draft for the Create Bot form. A new Bot starts stopped (the
// server defaults to it and rejects "running"), has no provider profile or
// model selected, and applies no capability preset — nothing is pre-filled
// from a global/default provider.
const EMPTY_CREATE_DRAFT = {
  id: '', name: '', role: '',
  workspace_id: '', status: 'stopped',
  provider_profile_id: '', model: '', preset: '', allowlist: '',
};

// Split a free-text domain field into bare hostnames. Commas, whitespace, and
// newlines all separate entries; blanks are dropped. The server re-validates
// every entry (bare hostname only), so this only shapes the request body.
function parseDomainAllowlist(text) {
  return (text || '')
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

// Bot configuration surface. The primary CREATE action ("Create Bot") opens a
// form for a new owner-scoped Bot — identity, an exact provider profile/model,
// a capability preset, an optional browser allowlist, and a Rift (a new safe
// directory or a server-registered workspace). The server validates every
// field and returns a clear error, which is surfaced verbatim; a created Bot
// starts stopped and requires an explicit Start.
//
// Per-Bot configuration actions: "Configure as Developer Bot" shows the named
// preset's effective permissions and requires an explicit confirmation before
// calling the owner-scoped configure endpoint; "Configure LLM" is a SEPARATE
// action for the provider profile + model. Both keep failing closed (e.g. a
// Rift that is not a real repository), with the server's message surfaced
// verbatim.
//
// Legacy (ownerless) Bots are listed separately with a "Claim legacy Bot"
// action. Claiming is confirmed first and grants ownership ONLY — it never
// starts the Bot or changes its policy. After a claim the roster refreshes,
// so the freshly-owned Bot falls under the standard lifecycle + Configure
// controls. The server is authoritative on both the operator check and the
// ownerless-only rule.
export default function BotSettings({ bots = [], onClose, onChanged }) {
  const [presets, setPresets] = useState([]);
  const [pending, setPending] = useState(null); // the bot awaiting confirmation
  const [pendingClaim, setPendingClaim] = useState(null); // legacy bot awaiting claim confirmation
  // Coordinator configuration: the bot awaiting the explicit "enable coordination"
  // confirmation, and the in-flight busy id for that action. Kept separate from
  // the Developer flow so enabling coordination can never be conflated with
  // granting write capability.
  const [pendingCoordinator, setPendingCoordinator] = useState(null);
  const [coordinatorBusyId, setCoordinatorBusyId] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [claimBusyId, setClaimBusyId] = useState(null);
  const [lifecycleBusyId, setLifecycleBusyId] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // Per-Bot LLM configuration. `profiles` are the current user's encrypted
  // provider profiles (names/models only — never secrets). The draft is the
  // owner's in-progress {provider_profile_id, model} selection.
  const [profiles, setProfiles] = useState([]);
  const [llmEditingId, setLlmEditingId] = useState(null);
  const [llmDraft, setLlmDraft] = useState({ provider_profile_id: '', model: '' });
  const [llmBusyId, setLlmBusyId] = useState(null);
  // Create Bot: the form toggle, the in-progress draft, and the server-owned
  // workspace registry the Rift can be selected from (ids/names only — never
  // filesystem paths; a raw path can never be sent from here).
  const [creating, setCreating] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);
  const [createDraft, setCreateDraft] = useState(EMPTY_CREATE_DRAFT);
  const [advanced, setAdvanced] = useState(false);
  const [workspaces, setWorkspaces] = useState([]);
  // Per-Bot browser domain allowlist editing (owner-scoped PATCH). The draft
  // is the owner's in-progress free-text list of bare hostnames.
  const [allowlistEditingId, setAllowlistEditingId] = useState(null);
  const [allowlistDraft, setAllowlistDraft] = useState('');
  const [allowlistBusyId, setAllowlistBusyId] = useState(null);
  // Per-Bot Browser Host selection. `hostDraft` holds the server's owner-scoped
  // view: the current bound host id and the ELIGIBLE hosts (redacted views —
  // no secret, no CDP URL) the owner may pick from.
  const [hostEditingId, setHostEditingId] = useState(null);
  const [hostDraft, setHostDraft] = useState({ bound_host_id: '', hosts: [] });
  const [hostBusyId, setHostBusyId] = useState(null);
  // Browser Bot configuration: the bot awaiting the explicit "Configure as
  // Browser Bot" confirmation, the in-flight busy id, and the bot's OWN bound
  // host (fetched from server state when the confirmation opens) so the dialog
  // can show whether the explicit binding the preset requires is in place.
  const [pendingBrowser, setPendingBrowser] = useState(null);
  const [browserBusyId, setBrowserBusyId] = useState(null);
  const [browserHost, setBrowserHost] = useState({
    bound_host_id: '', loading: false,
  });

  useEffect(() => {
    listBotPresets()
      .then(setPresets)
      .catch((e) => setError(e.message));
    listProviderProfiles()
      .then((list) => {
        setProfiles(list);
        // Pre-fill the create form with the user's own default provider
        // profile + model — unless the user already chose one. This is the
        // "clear default provider/model" the basic flow relies on; with no
        // configured profile nothing is pre-filled (no global fallback).
        setCreateDraft((draft) => {
          if (draft.provider_profile_id || draft.model) return draft;
          const def = defaultSelection(list);
          return def
            ? { ...draft, provider_profile_id: def.provider_profile_id, model: def.model }
            : draft;
        });
      })
      .catch((e) => setError(e.message));
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([])); // best-effort; a new safe Rift still works
  }, []);

  const developer = presets.find((p) => p.id === 'developer');
  // The Coordinator preset — the host operation ("coordinate Bots") that makes
  // a Bot the owner's Chief of Staff. It is a SEPARATE capability from the
  // Developer preset and is never enabled implicitly.
  const coordinator = presets.find((p) => p.id === 'coordinator');
  // The Browser preset — read-only browsing (navigate + read) at the established
  // safe tiers. A SEPARATE capability from Developer and Coordinator, and it is
  // only enabled when the Bot has a non-empty allowlist and an explicit host.
  const browser = presets.find((p) => p.id === 'browser');
  const manageable = bots.filter((b) => b.manageable);
  // Visible but ownerless (legacy) Bots: the ONLY Bots offered a claim. A Bot
  // owned by someone else never reaches this list, so it can never be offered.
  const claimable = bots.filter((b) => !b.manageable && b.claimable);

  // One-time legacy claim. Claiming grants ownership only — it does not start
  // the Bot or change its policy. On success the roster is refreshed so the
  // freshly-owned Bot moves under the standard Start/Pause/Stop + Configure
  // controls (the server re-derives ownership on that refresh).
  const confirmClaim = async () => {
    if (!pendingClaim) return;
    const target = pendingClaim;
    setClaimBusyId(target.id);
    setError('');
    setNotice('');
    try {
      const updated = await claimBot(target.id);
      setNotice(
        `${updated.name || updated.id} is now yours. It was not started and its `
        + 'policy was not changed — use Start or Configure as Developer Bot below.'
      );
      setPendingClaim(null);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setClaimBusyId(null);
    }
  };

  const confirm = async () => {
    if (!pending || !developer) return;
    setBusyId(pending.id);
    setError('');
    setNotice('');
    try {
      const updated = await configureBot(pending.id, { preset: developer.id });
      setNotice(`${updated.name || updated.id} is now a Developer Bot.`);
      setPending(null);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  };

  // Owner-scoped "enable coordination". Sends ONLY the named coordinator preset
  // to the EXISTING configure endpoint (the server re-checks ownership and
  // returns 403 otherwise). This grants the coordination host operation and the
  // safe reads a coordinator needs — never write, browser, PR, push, delete, or
  // approval authority. On success the roster is refreshed so the Coordinator
  // badge reflects the server's own view (never an optimistic local guess).
  const confirmCoordinator = async () => {
    if (!pendingCoordinator || !coordinator) return;
    const target = pendingCoordinator;
    setCoordinatorBusyId(target.id);
    setError('');
    setNotice('');
    try {
      const updated = await configureBot(target.id, { preset: coordinator.id });
      setNotice(
        `${updated.name || updated.id} is now a Coordinator — it can delegate work `
        + 'to your eligible Bots. It gained no write, browser, or approval access.'
      );
      setPendingCoordinator(null);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setCoordinatorBusyId(null);
    }
  };

  // Open the "Configure as Browser Bot" confirmation and load the Bot's OWN
  // bound host from server state, so the dialog shows whether the explicit
  // Browser Host binding the preset requires is already in place. The server
  // re-checks binding + allowlist on the actual request (fail closed).
  const openBrowserConfirm = async (bot) => {
    setPendingBrowser(bot);
    setBrowserHost({ bound_host_id: '', loading: true });
    setError('');
    setNotice('');
    try {
      const info = await getBotBrowserHost(bot.id);
      setBrowserHost({
        bound_host_id: info.bound_host_id || '', loading: false,
      });
    } catch (e) {
      setBrowserHost({ bound_host_id: '', loading: false });
      setError(e.message);
    }
  };

  // Owner-scoped "Configure as Browser Bot". Sends ONLY the named browser
  // preset to the EXISTING configure endpoint (the server re-checks ownership
  // → 403, allowlist + host binding → 409). This grants navigation and page
  // reading at their safe tiers and NOTHING else — no click, type, submit,
  // upload, download, screenshot, file write/delete, repo PR/push, mail send,
  // calendar write, or coordination authority. On success the roster is
  // refreshed so the Browser Bot badge reflects the server's own view.
  const confirmBrowser = async () => {
    if (!pendingBrowser || !browser) return;
    const target = pendingBrowser;
    setBrowserBusyId(target.id);
    setError('');
    setNotice('');
    try {
      const updated = await configureBot(target.id, { preset: browser.id });
      setNotice(
        `${updated.name || updated.id} is now a Browser Bot — it can navigate `
        + 'and read pages on its bound host. It gained no click, type, submit, '
        + 'upload, download, screenshot, write, push, mail, or calendar access.'
      );
      setPendingBrowser(null);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBrowserBusyId(null);
    }
  };

  // Owner-scoped per-Bot LLM configuration. The profile reference + exact
  // model are validated server-side (model must belong to the profile); the
  // profile's secret never leaves the server, so nothing sensitive is sent
  // from here.
  const saveBotLlm = async (bot) => {
    setLlmBusyId(bot.id);
    setError('');
    setNotice('');
    try {
      const updated = await configureBot(bot.id, {
        provider_profile_id: llmDraft.provider_profile_id,
        model: llmDraft.model,
      });
      setNotice(
        `${updated.name || updated.id} now uses ${llmDraft.model} `
        + `via ${llmDraft.provider_profile_id}.`
      );
      setLlmEditingId(null);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setLlmBusyId(null);
    }
  };

  // Owner-scoped lifecycle transition. Start = eligible for new work; Pause/
  // Stop = reject new work. The server is authoritative (it re-checks
  // ownership) and this never claims a process was launched.
  const setLifecycle = async (bot, status) => {
    setLifecycleBusyId(bot.id);
    setError('');
    setNotice('');
    try {
      const updated = await updateBotStatus(bot.id, status);
      setNotice(lifecycleMessage(updated, status));
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setLifecycleBusyId(null);
    }
  };

  // Open/close the per-Bot browser domain allowlist editor. The current value
  // is the server's REDACTED allowlist (bare hostnames only).
  const openAllowlist = (bot) => {
    const next = allowlistEditingId === bot.id ? null : bot.id;
    setAllowlistEditingId(next);
    if (next) setAllowlistDraft((bot.browser_allowlist || []).join(', '));
    setError('');
    setNotice('');
  };

  // Owner-scoped allowlist save. The free-text field is split into bare
  // hostnames here; the server re-validates every entry and fails closed, so
  // this only shapes the request body (never the safety check).
  const saveAllowlist = async (bot) => {
    setAllowlistBusyId(bot.id);
    setError('');
    setNotice('');
    try {
      const updated = await updateBotAllowlist(
        bot.id, parseDomainAllowlist(allowlistDraft));
      const shown = (updated.browser_allowlist || []).join(', ') || 'none';
      setNotice(`${updated.name || updated.id} browser allowlist: ${shown}.`);
      setAllowlistEditingId(null);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setAllowlistBusyId(null);
    }
  };

  // Open/close the per-Bot Browser Host selector and load the owner-scoped
  // view: the bound host id plus the eligible hosts. A Bot may only be bound
  // to a host the SAME owner enrolled — the server is authoritative.
  const openHost = async (bot) => {
    if (hostEditingId === bot.id) {
      setHostEditingId(null);
      return;
    }
    setHostEditingId(bot.id);
    setHostBusyId(bot.id);
    setError('');
    setNotice('');
    try {
      const info = await getBotBrowserHost(bot.id);
      setHostDraft({
        bound_host_id: info.bound_host_id || '',
        hosts: info.hosts || [],
      });
    } catch (e) {
      setError(e.message);
      setHostDraft({ bound_host_id: '', hosts: [] });
    } finally {
      setHostBusyId(null);
    }
  };

  // Explicitly bind or unbind the Bot's Browser Host. There is NO implicit
  // host: selecting an empty option removes the binding (the Bot then has no
  // host and a browser task fails closed rather than running anywhere else).
  const selectHost = async (bot, hostId) => {
    setHostBusyId(bot.id);
    setError('');
    setNotice('');
    try {
      if (hostId) {
        const res = await bindBotBrowserHost(bot.id, hostId);
        setHostDraft((d) => ({ ...d, bound_host_id: res.bound_host_id || hostId }));
        setNotice(`${bot.name || bot.id} is bound to Browser Host ${hostId}.`);
      } else {
        await unbindBotBrowserHost(bot.id);
        setHostDraft((d) => ({ ...d, bound_host_id: '' }));
        setNotice(`${bot.name || bot.id} has no Browser Host bound.`);
      }
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setHostBusyId(null);
    }
  };

  // Create a new owner-scoped Bot. One call carries the whole contract; the
  // server validates it and fails closed with a clear message, which is
  // surfaced verbatim. On success the roster is refreshed so the new Bot
  // appears in the owner's list (started stopped, awaiting an explicit Start).
  const submitCreate = async () => {
    setCreateBusy(true);
    setError('');
    setNotice('');
    try {
      const created = await createBot({
        id: createDraft.id.trim() || undefined,
        name: createDraft.name.trim(),
        role: createDraft.role.trim() || undefined,
        model: createDraft.model.trim() || undefined,
        providerProfileId: createDraft.provider_profile_id || undefined,
        preset: createDraft.preset || undefined,
        browserAllowlist: parseDomainAllowlist(createDraft.allowlist),
        status: createDraft.status || 'stopped',
        workspaceId: createDraft.workspace_id || undefined,
      });
      setNotice(
        `${created.name || created.id} is ready — state: ${stateLabel(created.status)}. `
        + 'Use Start to make it Ready for new conversations and task work.'
      );
      setCreating(false);
      setCreateDraft(EMPTY_CREATE_DRAFT);
      setAdvanced(false);
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setCreateBusy(false);
    }
  };

  const permissionRows = developer
    ? Object.entries(developer.permissions || {}).sort(([a], [b]) =>
        a.localeCompare(b)
      )
    : [];

  // The coordinator preset's effective, host-derived permissions — shown in the
  // confirmation so the owner can SEE that coordination adds no write/browser/
  // delete/push capability (those remain "denied").
  const coordinatorRows = coordinator
    ? Object.entries(coordinator.permissions || {}).sort(([a], [b]) =>
        a.localeCompare(b)
      )
    : [];

  // The browser preset's effective, host-derived permissions — shown in the
  // confirmation so the owner can SEE that browse/read are granted while every
  // interaction/write capability stays "denied".
  const browserRows = browserBotPermissionRows(browser);

  return (
    <section className="provider-settings" aria-label="Bot settings">
      <div className="settings-heading">
        <div>
          <h2>Bots</h2>
          <p>
            Start a Bot to make it eligible for new Chat conversations and
            task work; Pause or Stop it to reject new work. Kyrex runs Bots on
            a shared worker — starting a Bot does not launch a separate
            process. Configure one you own as a Developer Bot to give it write
            capability; every write still goes through the existing approval
            flow. Or enable coordination to let it delegate work to your other
            Bots — a Coordinator gains no write, browser, or approval access of
            its own.
          </p>
        </div>
        <div className="bot-heading-actions">
          <button
            type="button"
            className="settings-close"
            onClick={() => {
              setCreating((c) => !c);
              setError('');
              setNotice('');
            }}
          >
            {creating ? 'Close Create' : 'Create Bot'}
          </button>
          <button type="button" className="settings-close" onClick={onClose}>
            Close
          </button>
        </div>
      </div>

      {error && (
        <div className="message-error" role="alert">
          {error}
        </div>
      )}
      {notice && <div className="bot-notice">{notice}</div>}

      {creating && (
        <div className="bot-confirm bot-create" role="dialog" aria-label="Create Bot">
          <h3>Create a Bot</h3>
          <p>
            Name your Bot and tell it what to do. Kyrex generates the Bot's id
            and workspace for you and picks a ready-to-use model from your
            provider settings. Browser access and write capability stay OFF
            until you enable them. No secret is ever sent or stored here.
          </p>

          <div className="bot-config-field">
            <label htmlFor="create-bot-name">Bot name</label>
            <input
              id="create-bot-name"
              type="text"
              placeholder="My Bot"
              value={createDraft.name}
              onChange={(e) => setCreateDraft({ ...createDraft, name: e.target.value })}
            />
          </div>

          <div className="bot-config-field">
            <label htmlFor="create-bot-prompt">What should this Bot do?</label>
            <textarea
              id="create-bot-prompt"
              rows={3}
              placeholder="You are a helpful assistant that…"
              value={createDraft.role}
              onChange={(e) => setCreateDraft({ ...createDraft, role: e.target.value })}
            />
          </div>

          <div className="bot-config-field">
            <label htmlFor="create-bot-model">Model</label>
            {profiles.length > 0 ? (
              <select
                id="create-bot-model"
                value={createDraft.model}
                onChange={(e) => setCreateDraft({ ...createDraft, model: e.target.value })}
              >
                {((profiles.find((p) => p.id === createDraft.provider_profile_id) || profiles[0]).models || [])
                  .map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
              </select>
            ) : (
              <input
                id="create-bot-model"
                type="text"
                placeholder="exact model name"
                value={createDraft.model}
                onChange={(e) => setCreateDraft({ ...createDraft, model: e.target.value })}
              />
            )}
            <span className="bot-config-hint">
              {profiles.length === 0
                ? 'No provider profile yet — add one under Provider settings. A Bot with no profile and no model cannot serve turns.'
                : `Using ${profileLabel(profiles, createDraft.provider_profile_id)} — change the profile under Advanced.`}
            </span>
          </div>

          <button
            type="button"
            className="settings-close bot-advanced-toggle"
            onClick={() => setAdvanced((a) => !a)}
          >
            {advanced ? 'Hide advanced' : 'Advanced'}
          </button>

          {advanced && (
            <div className="bot-advanced">
              <div className="bot-config-field">
                <label htmlFor="create-bot-id">Stable ID (optional)</label>
                <input
                  id="create-bot-id"
                  type="text"
                  placeholder="auto from the name"
                  value={createDraft.id}
                  onChange={(e) => setCreateDraft({ ...createDraft, id: e.target.value })}
                />
                <span className="bot-config-hint">
                  Leave blank to generate it from the name. Lowercase letters,
                  numbers, hyphens, underscores — must be unique.
                </span>
              </div>

              <div className="bot-config-field">
                <label htmlFor="create-bot-profile">Provider profile</label>
                <select
                  id="create-bot-profile"
                  value={createDraft.provider_profile_id}
                  onChange={(e) => {
                    const pid = e.target.value;
                    const prof = profiles.find((p) => p.id === pid);
                    setCreateDraft({
                      ...createDraft,
                      provider_profile_id: pid,
                      model: (prof && prof.models && prof.models[0]) || '',
                    });
                  }}
                >
                  <option value="">— select a profile —</option>
                  {profiles.map((p) => (
                    <option key={p.id} value={p.id}>{p.name || p.id}</option>
                  ))}
                </select>
              </div>

              <div className="bot-config-field">
                <label htmlFor="create-bot-workspace">Rift / workspace</label>
                <select
                  id="create-bot-workspace"
                  value={createDraft.workspace_id}
                  onChange={(e) => setCreateDraft({ ...createDraft, workspace_id: e.target.value })}
                >
                  <option value="">— create a new safe Rift —</option>
                  {workspaces.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name || w.id}{w.available === false ? ' (unavailable)' : ''}
                    </option>
                  ))}
                </select>
                <span className="bot-config-hint">
                  Created for you by default. A Rift is chosen by name from the
                  server registry — a filesystem path is never accepted.
                </span>
              </div>

              <div className="bot-config-field">
                <label htmlFor="create-bot-status">Initial status</label>
                <select
                  id="create-bot-status"
                  value={createDraft.status}
                  onChange={(e) => setCreateDraft({ ...createDraft, status: e.target.value })}
                >
                  <option value="stopped">Stopped (start it later)</option>
                  <option value="paused">Paused</option>
                </select>
              </div>

              <div className="bot-config-field">
                <label htmlFor="create-bot-preset">Capability</label>
                <select
                  id="create-bot-preset"
                  value={createDraft.preset}
                  onChange={(e) => setCreateDraft({ ...createDraft, preset: e.target.value })}
                >
                  <option value="">Default (read-only)</option>
                  <option value="developer">Developer Bot (write capability)</option>
                </select>
              </div>

              <div className="bot-config-field">
                <label htmlFor="create-bot-allowlist">Browser domain allowlist (optional)</label>
                <input
                  id="create-bot-allowlist"
                  type="text"
                  placeholder="example.com, docs.example.com"
                  value={createDraft.allowlist}
                  onChange={(e) => setCreateDraft({ ...createDraft, allowlist: e.target.value })}
                />
                <span className="bot-config-hint">
                  Browser access is OFF by default. Bare hostnames only; empty
                  means the Browser Operator denies every navigation.
                </span>
              </div>
            </div>
          )}

          <div className="bot-confirm-actions">
            <button
              type="button"
              className="send-btn"
              disabled={createBusy || !createDraft.name.trim()}
              onClick={submitCreate}
            >
              {createBusy ? 'Creating…' : 'Create Bot'}
            </button>
            <button
              type="button"
              className="settings-close"
              disabled={createBusy}
              onClick={() => {
                setCreating(false);
                setCreateDraft(EMPTY_CREATE_DRAFT);
                setAdvanced(false);
                setError('');
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {claimable.length > 0 && (
        <div className="bot-legacy">
          <h3>Legacy Bots</h3>
          <p>
            These Bots have no owner yet, so they are visible but cannot be
            managed. Claim one to take control of it.
          </p>
          <div className="provider-list">
            {claimable.map((bot) => {
              const status = bot.status || 'stopped';
              const claiming = claimBusyId === bot.id;
              return (
                <div key={bot.id} className="provider-row">
                  <div>
                    <strong>{bot.name || bot.id}</strong>
                    <span>{bot.model || 'no model set'}</span>
                    <span className={`bot-status bot-status-${status}`}>
                      {bot.available === false
                        ? 'Rift unavailable'
                        : stateLabel(status)}
                    </span>
                    <span className="bot-legacy-tag">ownerless</span>
                  </div>
                  <button
                    type="button"
                    className="bot-claim-btn"
                    disabled={claiming}
                    onClick={() => {
                      setPendingClaim(bot);
                      setError('');
                      setNotice('');
                    }}
                  >
                    {claiming ? 'Claiming…' : 'Claim legacy Bot'}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {manageable.length === 0 && claimable.length === 0 ? (
        <div className="bot-empty">
          You don’t own any Bots yet. Create one to configure it.
        </div>
      ) : manageable.length === 0 ? (
        <div className="bot-empty">
          You don’t own any Bots yet. Claim a legacy Bot above to manage it.
        </div>
      ) : (
        <div className="provider-list">
          {manageable.map((bot) => {
            const busy = lifecycleBusyId === bot.id;
            const status = bot.status || 'stopped';
            const editingLlm = llmEditingId === bot.id;
            const editingAllowlist = allowlistEditingId === bot.id;
            const editingHost = hostEditingId === bot.id;
            const profile = profiles.find((p) => p.id === llmDraft.provider_profile_id);
            return (
              <React.Fragment key={bot.id}>
              <div className="provider-row">
                <div>
                  <strong>{bot.name || bot.id}</strong>
                  <span>{bot.model || 'no model set'}</span>
                  <span className={`bot-status bot-status-${status}`}>
                    {bot.available === false
                      ? 'Rift unavailable'
                      : stateLabel(status)}
                  </span>
                  {bot.coordinator && (
                    <span
                      className="bot-coordinator-tag"
                      title="This Bot can delegate work to your eligible Bots. It cannot approve another Bot's actions or inherit its credentials, browser sessions, provider keys, Rift, or write permissions."
                    >
                      Coordinator
                    </span>
                  )}
                  {browserBotBadge(bot) && (
                    <span
                      className="bot-browser-tag"
                      title="This Bot is a read-only Browser Bot: it may navigate and read pages on its bound host. It cannot click, type, submit, upload, download, take screenshots, write or delete files, open PRs, push, send mail, or write to your calendar."
                    >
                      {BROWSER_BOT_BADGE_LABEL}
                    </span>
                  )}
                  <div
                    className="bot-lifecycle"
                    role="group"
                    aria-label={`Lifecycle controls for ${bot.name || bot.id}`}
                  >
                    <button
                      type="button"
                      className="bot-lifecycle-btn"
                      disabled={busy || status === 'running'}
                      title="Make this Bot eligible for new Chat and task work. Kyrex uses a shared worker — no separate process is started."
                      onClick={() => setLifecycle(bot, 'running')}
                    >
                      Start
                    </button>
                    <button
                      type="button"
                      className="bot-lifecycle-btn"
                      disabled={busy || status === 'paused'}
                      title="Reject new turns and tasks. Work already running continues to completion."
                      onClick={() => setLifecycle(bot, 'paused')}
                    >
                      Pause
                    </button>
                    <button
                      type="button"
                      className="bot-lifecycle-btn"
                      disabled={busy || status === 'stopped'}
                      title="Reject new turns and tasks. Work already running continues to completion."
                      onClick={() => setLifecycle(bot, 'stopped')}
                    >
                      Stop
                    </button>
                    {busy && (
                      <span className="bot-lifecycle-busy" aria-live="polite">
                        Updating…
                      </span>
                    )}
                  </div>
                </div>
                <div className="bot-actions">
                  <button
                    type="button"
                    className="bot-configure-btn"
                    disabled={!developer || busyId === bot.id}
                    onClick={() => {
                      setPending(bot);
                      setError('');
                      setNotice('');
                    }}
                  >
                    Configure as Developer Bot
                  </button>
                  <button
                    type="button"
                    className="bot-configure-btn"
                    disabled={!coordinator || coordinatorBusyId === bot.id}
                    title="Enable coordination: let this Bot delegate work to your eligible Bots. It gains no write, browser, or approval access."
                    onClick={() => {
                      setPendingCoordinator(bot);
                      setError('');
                      setNotice('');
                    }}
                  >
                    {bot.coordinator
                      ? 'Reconfigure coordination'
                      : 'Configure as Coordinator'}
                  </button>
                  <button
                    type="button"
                    className="bot-configure-btn"
                    disabled={!browser || browserBusyId === bot.id}
                    title="Configure this Bot as a read-only Browser Bot: it may navigate and read pages on a Browser Host you bind it to. Requires a non-empty browser allowlist and an explicit Browser Host binding."
                    onClick={() => openBrowserConfirm(bot)}
                  >
                    {browserBotBadge(bot)
                      ? 'Reconfigure as Browser Bot'
                      : 'Configure as Browser Bot'}
                  </button>
                  <button
                    type="button"
                    className="bot-configure-btn"
                    disabled={llmBusyId === bot.id}
                    title="Choose the provider profile and exact model this Bot runs with."
                    onClick={() => {
                      setLlmEditingId(editingLlm ? null : bot.id);
                      setLlmDraft({
                        provider_profile_id: bot.provider_profile_id || '',
                        model: bot.model || '',
                      });
                      setError('');
                      setNotice('');
                    }}
                  >
                    {editingLlm ? 'Close LLM setup' : 'Configure LLM'}
                  </button>
                  <button
                    type="button"
                    className="bot-configure-btn"
                    disabled={allowlistBusyId === bot.id}
                    title="Edit the bare-hostname allowlist the Browser Operator enforces on every navigation."
                    onClick={() => openAllowlist(bot)}
                  >
                    {editingAllowlist ? 'Close allowlist' : 'Browser allowlist'}
                  </button>
                  <button
                    type="button"
                    className="bot-configure-btn"
                    disabled={hostBusyId === bot.id}
                    title="Choose the Browser Host this Bot runs on. There is no implicit host — a bound Bot runs only on the host you pick."
                    onClick={() => openHost(bot)}
                  >
                    {editingHost ? 'Close host setup' : 'Browser Host'}
                  </button>
                </div>
              </div>
              {editingLlm && (
                <div className="bot-llm-config">
                  <div className="bot-config-field">
                    <label htmlFor={`llm-profile-${bot.id}`}>Provider profile</label>
                    <select
                      id={`llm-profile-${bot.id}`}
                      value={llmDraft.provider_profile_id}
                      onChange={(e) => setLlmDraft({
                        provider_profile_id: e.target.value,
                        model: '',
                      })}
                    >
                      <option value="">— select a profile —</option>
                      {profiles.map((p) => (
                        <option key={p.id} value={p.id}>{p.name || p.id}</option>
                      ))}
                    </select>
                  </div>
                  <div className="bot-config-field">
                    <label htmlFor={`llm-model-${bot.id}`}>Model</label>
                    <select
                      id={`llm-model-${bot.id}`}
                      value={llmDraft.model}
                      disabled={!profile}
                      onChange={(e) => setLlmDraft({ ...llmDraft, model: e.target.value })}
                    >
                      <option value="">— select a model —</option>
                      {(profile ? profile.models : []).map((m) => (
                        <option key={m} value={m}>{m}</option>
                      ))}
                    </select>
                  </div>
                  <button
                    type="button"
                    className="send-btn"
                    disabled={!llmDraft.provider_profile_id
                      || !llmDraft.model || llmBusyId === bot.id}
                    onClick={() => saveBotLlm(bot)}
                  >
                    {llmBusyId === bot.id ? 'Saving…' : 'Save LLM configuration'}
                  </button>
                  <span className="bot-config-hint">
                    {profiles.length === 0
                      ? 'No provider profiles yet — add one under Provider settings.'
                      : 'A Bot with no provider profile cannot serve turns.'}
                  </span>
                </div>
              )}
              {editingAllowlist && (
                <div className="bot-llm-config">
                  <div className="bot-config-field">
                    <label htmlFor={`allow-${bot.id}`}>Browser domain allowlist</label>
                    <input
                      id={`allow-${bot.id}`}
                      type="text"
                      placeholder="example.com, docs.example.com"
                      value={allowlistDraft}
                      onChange={(e) => setAllowlistDraft(e.target.value)}
                    />
                    <span className="bot-config-hint">
                      Bare hostnames only; empty means the Browser Operator
                      denies every navigation. Currently:{' '}
                      {(bot.browser_allowlist || []).join(', ') || 'none'}.
                    </span>
                  </div>
                  <button
                    type="button"
                    className="send-btn"
                    disabled={allowlistBusyId === bot.id}
                    onClick={() => saveAllowlist(bot)}
                  >
                    {allowlistBusyId === bot.id ? 'Saving…' : 'Save allowlist'}
                  </button>
                </div>
              )}
              {editingHost && (
                <div className="bot-llm-config">
                  <div className="bot-config-field">
                    <label htmlFor={`host-${bot.id}`}>Browser Host</label>
                    <select
                      id={`host-${bot.id}`}
                      value={hostDraft.bound_host_id || ''}
                      disabled={hostBusyId === bot.id}
                      onChange={(e) => selectHost(bot, e.target.value)}
                    >
                      <option value="">— no host (browser access off) —</option>
                      {(hostDraft.hosts || []).map((h) => (
                        <option key={h.host_id} value={h.host_id}>
                          {h.host_id} — {h.state}{h.available ? ' (online)' : ''}
                        </option>
                      ))}
                    </select>
                    <span className="bot-config-hint">
                      A bound Browser Bot runs only on the host you select here;
                      there is no implicit host. Selected host must belong to
                      you.{' '}
                      {(hostDraft.hosts || []).length === 0
                        ? 'No Browser Hosts enrolled yet.'
                        : ''}
                    </span>
                  </div>
                </div>
              )}
              </React.Fragment>
            );
          })}
        </div>
      )}

      {pendingClaim && (
        <div className="bot-confirm" role="dialog" aria-label="Confirm legacy Bot claim">
          <h3>Claim “{pendingClaim.name || pendingClaim.id}”?</h3>
          <p>
            This makes you the owner of this legacy Bot so you can manage it.
            Claiming <strong>grants control only</strong> — it does not start
            the Bot and does not change its policy. Once claimed, the Bot is
            yours alone: nobody else can manage it.
          </p>
          <div className="bot-confirm-actions">
            <button
              type="button"
              className="send-btn"
              disabled={claimBusyId === pendingClaim.id}
              onClick={confirmClaim}
            >
              {claimBusyId === pendingClaim.id ? 'Claiming…' : 'Claim this Bot'}
            </button>
            <button
              type="button"
              className="settings-close"
              disabled={claimBusyId === pendingClaim.id}
              onClick={() => setPendingClaim(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {pending && developer && (
        <div className="bot-confirm" role="dialog" aria-label="Confirm Developer Bot">
          <h3>Make “{pending.name || pending.id}” a Developer Bot?</h3>
          <p>
            This grants the Bot write capability. Every write, PR, delete, or
            push is still decided by the existing approval flow — nothing is
            auto-approved.
          </p>
          <div className="perm-table" role="table" aria-label="Effective permissions">
            {permissionRows.map(([op, tier]) => {
              const view = permissionView(tier);
              return (
                <div className="perm-row" role="row" key={op}>
                  <span className="perm-op">{op}</span>
                  <span className={`perm-val ${view.cls}`}>{view.text}</span>
                </div>
              );
            })}
          </div>
          <div className="bot-confirm-actions">
            <button
              type="button"
              className="send-btn"
              disabled={busyId === pending.id}
              onClick={confirm}
            >
              {busyId === pending.id ? 'Configuring…' : 'Confirm'}
            </button>
            <button
              type="button"
              className="settings-close"
              disabled={busyId === pending.id}
              onClick={() => setPending(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {pendingCoordinator && coordinator && (
        <div className="bot-confirm" role="dialog" aria-label="Confirm Coordinator Bot">
          <h3>Enable coordination for “{pendingCoordinator.name || pendingCoordinator.id}”?</h3>
          <p>
            A Coordinator can delegate work to your eligible Bots and receive
            safe status/results. It cannot approve another Bot’s actions or
            inherit its credentials, browser sessions, provider keys, Rift, or
            write permissions.
          </p>
          <p>
            This is separate from Developer configuration and grants no
            filesystem write, PR, browser, mail, calendar, delete, or push
            capability. Every delegated action is still executed (and approved)
            by the target Bot under its own policy.
          </p>
          <div className="perm-table" role="table" aria-label="Effective coordinator permissions">
            {coordinatorRows.map(([op, tier]) => {
              const view = permissionView(tier);
              return (
                <div className="perm-row" role="row" key={op}>
                  <span className="perm-op">{op}</span>
                  <span className={`perm-val ${view.cls}`}>{view.text}</span>
                </div>
              );
            })}
          </div>
          <div className="bot-confirm-actions">
            <button
              type="button"
              className="send-btn"
              disabled={coordinatorBusyId === pendingCoordinator.id}
              onClick={confirmCoordinator}
            >
              {coordinatorBusyId === pendingCoordinator.id
                ? 'Configuring…'
                : 'Enable coordination'}
            </button>
            <button
              type="button"
              className="settings-close"
              disabled={coordinatorBusyId === pendingCoordinator.id}
              onClick={() => setPendingCoordinator(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {pendingBrowser && browser && (
        <div className="bot-confirm" role="dialog" aria-label="Configure as Browser Bot">
          <h3>Configure “{pendingBrowser.name || pendingBrowser.id}” as a Browser Bot?</h3>
          <p>
            A Browser Bot can do exactly two things on a Browser Host you bind it
            to: <strong>navigate</strong> to a page and <strong>read</strong> it.
            Every other capability stays off — it cannot click, type, submit,
            upload, download, take screenshots, write or delete files, open PRs,
            push, send mail, or write to your calendar.
          </p>
          <p className="bot-config-hint">
            Enabling requires a non-empty browser domain allowlist AND an explicit
            Browser Host binding. The server re-checks both and refuses otherwise.
          </p>
          {(() => {
            const blockers = browserBotBlockers(pendingBrowser, browserHost);
            if (blockers.length === 0) {
              return (
                <p className="bot-config-hint">
                  Ready: allowlist and Browser Host binding are in place.
                </p>
              );
            }
            return (
              <p className="bot-config-hint">
                {browserHost.loading
                  ? 'Checking the Browser Host binding… Missing: '
                  : 'Missing: '}
                {blockers.join(' and ')}.
              </p>
            );
          })()}
          <div className="perm-table" role="table" aria-label="Effective browser permissions">
            {browserRows.map(([op, tier]) => {
              const view = permissionView(tier);
              return (
                <div className="perm-row" role="row" key={op}>
                  <span className="perm-op">{op}</span>
                  <span className={`perm-val ${view.cls}`}>{view.text}</span>
                </div>
              );
            })}
          </div>
          <div className="bot-confirm-actions">
            <button
              type="button"
              className="send-btn"
              disabled={browserBusyId === pendingBrowser.id
                || browserHost.loading
                || !canEnableBrowserBot(pendingBrowser, browserHost)}
              onClick={confirmBrowser}
            >
              {browserBusyId === pendingBrowser.id ? 'Configuring…' : 'Confirm'}
            </button>
            <button
              type="button"
              className="settings-close"
              disabled={browserBusyId === pendingBrowser.id}
              onClick={() => setPendingBrowser(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
