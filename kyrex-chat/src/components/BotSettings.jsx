import React, { useEffect, useState } from 'react';
import { claimBot, configureBot, listBotPresets, updateBotStatus } from '../lib/api.js';

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

// Bot configuration surface. The primary action is "Configure as Developer
// Bot": it shows the named preset's effective permissions and requires an
// explicit confirmation before calling the owner-scoped configure endpoint.
// The server still fails closed (e.g. a Rift that is not a real repository),
// whose message is surfaced verbatim.
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

  useEffect(() => {
    listBotPresets()
      .then(setPresets)
      .catch((e) => setError(e.message));
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
        <button type="button" className="settings-close" onClick={onClose}>
          Close
        </button>
      </div>

      {error && (
        <div className="message-error" role="alert">
          {error}
        </div>
      )}
      {notice && <div className="bot-notice">{notice}</div>}

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
              </div>
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
