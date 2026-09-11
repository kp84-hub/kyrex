import React, { useEffect, useState } from 'react';
import { configureBot, listBotPresets } from '../lib/api.js';

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
export default function BotSettings({ bots = [], onClose, onChanged }) {
  const [presets, setPresets] = useState([]);
  const [pending, setPending] = useState(null); // the bot awaiting confirmation
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    listBotPresets()
      .then(setPresets)
      .catch((e) => setError(e.message));
  }, []);

  const developer = presets.find((p) => p.id === 'developer');
  const manageable = bots.filter((b) => b.manageable);

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
            Configure a Bot you own as a Developer Bot. Writes always go
            through the existing approval flow.
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

      {manageable.length === 0 ? (
        <div className="bot-empty">
          You don’t own any Bots yet. Create one to configure it.
        </div>
      ) : (
        <div className="provider-list">
          {manageable.map((bot) => (
            <div key={bot.id} className="provider-row">
              <div>
                <strong>{bot.name || bot.id}</strong>
                <span>{bot.model || 'no model set'}</span>
                <span>
                  {bot.available === false
                    ? 'Rift unavailable'
                    : `status: ${bot.status || 'unknown'}`}
                </span>
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
          ))}
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
