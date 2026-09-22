import React, { useEffect, useMemo, useState } from 'react';
import {
  connectGoogle, disconnectGoogle, fetchConnections, upgradeGoogleCalendarWrite,
} from '../lib/api.js';
import {
  CONNECT_LABEL, DISCONNECT_LABEL, READ_ONLY_NOTICE, UNAVAILABLE_NOTICE,
  WRITE_ENABLED_NOTICE, calendarReaderSummary, joinCapabilities, safeText,
  statusClassOf, statusLabelOf,
} from '../lib/connections.js';
import {
  COMING_SOON_LABEL, READ_ONLY_BADGE, READ_WRITE_BADGE, SECTION_AVAILABLE,
  SECTION_CONNECTED, SECRET_FREE_NOTICE, WRITE_UPGRADE_NOTICE, buildHubModel,
} from '../lib/connectorRegistry.js';

// Settings -> Connections: a Muse-style hub.
//
// The hub is driven ENTIRELY by the generic registry/card model
// (../lib/connectorRegistry.js): a search bar over a list split into
// "Connected" and "Available" sections. Google Calendar is the real OAuth
// integration; an unimplemented app (e.g. Gmail) appears as a non-connectable
// "Coming soon" card, never as something you can connect.
//
// Every dynamic value comes from the backend's REDACTED public view, is copied
// through the card model's allow-list, and is scrubbed by safeText — no token,
// client secret, or authorization code can reach the DOM. Reading is
// read-only; calendar event creation is a SEPARATE, explicitly approval-gated
// upgrade that never happens merely by connecting.
export default function ConnectionsSettings({ onClose }) {
  const [views, setViews] = useState([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [available, setAvailable] = useState(true);

  const refresh = async () => {
    setError('');
    try {
      const data = await fetchConnections();
      setViews((data && data.connectors) || []);
      setAvailable(true);
    } catch (e) {
      if (e && e.status === 503) {
        setAvailable(false);
      } else {
        setError(safeText(e && e.message));
      }
    }
  };
  useEffect(() => { refresh(); }, []);

  const hub = useMemo(() => buildHubModel(views, query), [views, query]);
  const google = views.find((v) => v && v.provider === 'google') || null;

  const openConsent = (started) => {
    // Only the provider consent URL is opened (with the single-use, owner-bound
    // state); the OAuth callback completes server-side and only the resulting
    // status is shown here — nothing secret transits this client.
    if (started && started.authorization_url) {
      window.open(started.authorization_url, '_blank', 'noopener');
    }
  };

  const run = async (kind, fn) => {
    setBusy(kind);
    setError('');
    try {
      openConsent(await fn());
      await refresh();
    } catch (e) {
      if (e && e.status === 503) setAvailable(false);
      else setError(safeText(e && e.message));
    } finally {
      setBusy('');
    }
  };

  const connect = () => run('connect', () => connectGoogle());
  const disconnect = () => run('disconnect', () => disconnectGoogle());
  const upgradeWrite = () => run('write', () => upgradeGoogleCalendarWrite());

  const renderCard = (card) => {
    const isGoogleCalendar = card.id === 'google_calendar';
    const granted = card.status === 'connected' || card.status === 'expired';
    return (
      <div
        key={card.id}
        className={`connection-card connector-card${card.connectable ? '' : ' planned'}`}
        aria-label={`${card.name} connector`}
      >
        <div className="connection-head">
          <span className="connector-icon" aria-hidden="true">{card.icon}</span>
          <span className="connector-identity">
            <strong>{card.name}</strong>
            <span className="connector-category">{card.category}</span>
          </span>
          <span className={`connection-status ${statusClassOf(card.status)}`}>
            {card.connectable ? statusLabelOf(card.status) : COMING_SOON_LABEL}
          </span>
        </div>

        <p className="connection-detail">{card.description}</p>

        <div className="connector-badges">
          <span className={`access-badge ${card.hasWriteScope ? 'write' : 'read'}`}>
            {card.hasWriteScope ? READ_WRITE_BADGE : READ_ONLY_BADGE}
          </span>
        </div>

        {isGoogleCalendar ? (
          <ul className="capability-list">
            {calendarReaderSummary(google).map((s) => (
              <li key={s.bot} className="capability-row">
                <strong>{s.bot}</strong>
                <span>
                  {safeText(joinCapabilities(s.capabilities)) || 'no capabilities declared'}
                  {' — read-only'}
                </span>
                <em>cannot: {safeText(joinCapabilities(s.unsupported)) || 'nothing else declared'}</em>
              </li>
            ))}
          </ul>
        ) : null}

        <div className="connection-actions">
          {card.connectable ? (
            <>
              {card.status === 'connected' ? (
                <button
                  type="button"
                  className="connection-btn secondary"
                  disabled={busy === 'disconnect'}
                  onClick={disconnect}
                >
                  {DISCONNECT_LABEL}
                </button>
              ) : (
                <button
                  type="button"
                  className="connection-btn primary"
                  disabled={busy === 'connect'}
                  onClick={connect}
                >
                  {card.status === 'expired' ? 'Reconnect Google Calendar' : CONNECT_LABEL}
                </button>
              )}
              <button
                type="button"
                className="connection-btn"
                disabled={Boolean(busy)}
                onClick={refresh}
              >
                Check connection
              </button>
            </>
          ) : (
            // An unimplemented app is NEVER connectable: it shows a disabled
            // placeholder and no Connect control at all.
            <button type="button" className="connection-btn" disabled>
              {COMING_SOON_LABEL}
            </button>
          )}
        </div>

        {isGoogleCalendar && card.writeUpgrade && granted ? (
          <div className="write-access" aria-label="Calendar write access">
            <div className="write-access-head">
              <strong>{card.writeUpgrade.label}</strong>
            </div>
            <p className="connection-notice">{WRITE_UPGRADE_NOTICE}</p>
            {card.hasWriteScope ? (
              <p className="connection-notice secure">{WRITE_ENABLED_NOTICE}</p>
            ) : (
              <div className="connection-actions">
                <button
                  type="button"
                  className="connection-btn"
                  disabled={busy === 'write'}
                  onClick={upgradeWrite}
                >
                  {card.writeUpgrade.label}
                </button>
              </div>
            )}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <section
      className="provider-settings connections-hub"
      aria-label="Connections"
    >
      <div className="settings-heading">
        <div>
          <h2>Connections</h2>
          <p>Link external accounts so your Bots can use them.</p>
        </div>
        {onClose && (
          <button type="button" className="settings-close" onClick={onClose}>Close</button>
        )}
      </div>

      <input
        type="search"
        className="connections-search"
        placeholder="Search connections"
        aria-label="Search connections"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {!available ? (
        <div className="message-error">{UNAVAILABLE_NOTICE}</div>
      ) : (
        <>
          {hub.connected.length ? (
            <section className="connections-section" aria-label={SECTION_CONNECTED}>
              <h3 className="connections-section-title">{SECTION_CONNECTED}</h3>
              <div className="connections-grid">{hub.connected.map(renderCard)}</div>
            </section>
          ) : null}

          {hub.available.length ? (
            <section className="connections-section" aria-label={SECTION_AVAILABLE}>
              <h3 className="connections-section-title">{SECTION_AVAILABLE}</h3>
              <div className="connections-grid">{hub.available.map(renderCard)}</div>
            </section>
          ) : null}

          {hub.isEmpty ? (
            <div className="connections-empty">
              No connections match “{safeText(query)}”.
            </div>
          ) : null}

          <p className="connection-notice">{READ_ONLY_NOTICE}</p>
          <p className="connection-notice secure">{SECRET_FREE_NOTICE}</p>
          {error ? <div className="message-error">{error}</div> : null}
        </>
      )}
    </section>
  );
}
