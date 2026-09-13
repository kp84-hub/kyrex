import React, { useEffect, useState } from 'react';
import {
  claimBot, configureBot, createBot, listBotPresets, listProviderProfiles,
  listWorkspaces, updateBotStatus,
} from '../lib/api.js';

// Lifecycle labels. A Bot's status is a work-eligibility label on the shared
// Kyrex worker — it is never a separate process. "running" admits new Chat
// conversations/tasks; "paused" and "stopped" reject new work (already-running
// work is not interrupted).
const LIFECYCLE_LABEL = {
  running: 'Running',
  paused: 'Paused',
  stopped: 'Stopped',
};

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
  id: '', name: '', system_prompt: '',
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
  const [workspaces, setWorkspaces] = useState([]);

  useEffect(() => {
    listBotPresets()
      .then(setPresets)
      .catch((e) => setError(e.message));
    listProviderProfiles()
      .then(setProfiles)
      .catch((e) => setError(e.message));
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([])); // best-effort; a new safe Rift still works
  }, []);

  const developer = presets.find((p) => p.id === 'developer');
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
        id: createDraft.id.trim(),
        name: createDraft.name.trim(),
        model: createDraft.model.trim(),
        providerProfileId: createDraft.provider_profile_id || undefined,
        systemPrompt: createDraft.system_prompt || undefined,
        preset: createDraft.preset || undefined,
        browserAllowlist: parseDomainAllowlist(createDraft.allowlist),
        status: createDraft.status || 'stopped',
        workspaceId: createDraft.workspace_id || undefined,
      });
      setNotice(
        `${created.name || created.id} was created as stopped. `
        + 'Use Start to make it eligible for new work.'
      );
      setCreating(false);
      setCreateDraft(EMPTY_CREATE_DRAFT);
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
            flow.
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
            Give the Bot an identity, an owner-scoped provider profile and the
            exact model it runs with, and optional capability and browser-domain
            settings. A new Bot starts stopped — you decide when to Start. No
            secret is ever sent or stored here: only the profile reference.
          </p>

          <div className="bot-config-field">
            <label htmlFor="create-bot-name">Bot name</label>
            <input
              id="create-bot-name"
              type="text"
              value={createDraft.name}
              onChange={(e) => setCreateDraft({ ...createDraft, name: e.target.value })}
            />
          </div>

          <div className="bot-config-field">
            <label htmlFor="create-bot-id">Stable ID</label>
            <input
              id="create-bot-id"
              type="text"
              placeholder="my-bot"
              value={createDraft.id}
              onChange={(e) => setCreateDraft({ ...createDraft, id: e.target.value })}
            />
            <span className="bot-config-hint">
              Lowercase letters, numbers, hyphens, underscores — must be unique.
            </span>
          </div>

          <div className="bot-config-field">
            <label htmlFor="create-bot-prompt">System prompt / identity</label>
            <textarea
              id="create-bot-prompt"
              rows={3}
              value={createDraft.system_prompt}
              onChange={(e) => setCreateDraft({ ...createDraft, system_prompt: e.target.value })}
            />
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
              A Rift is chosen by name from the server registry — a filesystem
              path is never accepted.
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
            <label htmlFor="create-bot-profile">Provider profile</label>
            <select
              id="create-bot-profile"
              value={createDraft.provider_profile_id}
              onChange={(e) => setCreateDraft({
                ...createDraft,
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
            <label htmlFor="create-bot-model">Exact model</label>
            {createDraft.provider_profile_id ? (
              <select
                id="create-bot-model"
                value={createDraft.model}
                onChange={(e) => setCreateDraft({ ...createDraft, model: e.target.value })}
              >
                <option value="">— select a model —</option>
                {(profiles.find((p) => p.id === createDraft.provider_profile_id)?.models || [])
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
                ? 'No provider profiles yet — a Bot with none stays unconfigured and cannot serve turns.'
                : 'A model must belong to the selected provider profile.'}
            </span>
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
              Bare hostnames only. Empty means the Browser Operator denies every
              navigation.
            </span>
          </div>

          <div className="bot-confirm-actions">
            <button
              type="button"
              className="send-btn"
              disabled={
                createBusy
                || !createDraft.id.trim()
                || !createDraft.name.trim()
                || !createDraft.model.trim()
              }
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
                        : `status: ${LIFECYCLE_LABEL[status] || status}`}
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
                      : `status: ${LIFECYCLE_LABEL[status] || status}`}
                  </span>
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
    </section>
  );
}
