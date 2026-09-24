import React, { useEffect, useMemo, useState } from 'react';
import {
  connectGoogle, disconnectGoogle, fetchConnections, upgradeGoogleCalendarWrite,
  upgradeGoogleGmailRead,
} from '../lib/api.js';
import {
  CONNECT_GMAIL_LABEL, CONNECT_LABEL, DISCONNECT_LABEL, GMAIL_READ_NOTICE,
  READ_ONLY_NOTICE, RECONNECT_GMAIL_LABEL, UNAVAILABLE_NOTICE,
  WRITE_ENABLED_NOTICE, calendarReaderSummary, gmailReaderSummary,
  joinCapabilities, safeText, statusClassOf, statusLabelOf,
} from '../lib/connections.js';
import {
  COMING_SOON_LABEL, READ_ONLY_BADGE, READ_WRITE_BADGE, SECTION_AVAILABLE,
  SECTION_CONNECTED, SECRET_FREE_NOTICE, WRITE_UPGRADE_NOTICE, buildHubModel,
} from '../lib/connectorRegistry.js';

// Settings -> Connections: a Muse-style hub.
//
// The hub is driven ENTIRELY by the generic registry/card model
// (../lib/connectorRegistry.js): a search bar over a list split into
// "Connected" and "Available" sections. Google Calendar is the read+write
// OAuth integration; Gmail is now the READ-ONLY integration and is
// connectable ONLY when the backend advertises the gmail.read path (otherwise
// it degrades to a non-connectable "Coming soon" card). Neither connector can
// inherit the other's grant -- they share one "google" provider record but
// each gates on its OWN scope.
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

  // Per-connector actions. Gmail uses its OWN read upgrade (adds the
  // gmail.readonly scope and PRESERVES Calendar); Calendar uses its plain
  // connect. Only a connector with a real disconnect offers one — Gmail shares
  // the one google token, so it exposes no destructive control here.
  const CONNECTOR_ACTIONS = {
    google_calendar: {
      connect: () => connectGoogle(),
      disconnect: () => disconnectGoogle(),
      label: CONNECT_LABEL,
      reconnect: 'Reconnect Google Calendar',
      connectedNote: '',
    },
    gmail: {
      connect: () => upgradeGoogleGmailRead(),
      disconnect: null,
      label: CONNECT_GMAIL_LABEL,
      reconnect: RECONNECT_GMAIL_LABEL,
      connectedNote: 'Gmail read is enabled (read-only).',
    },
  };

  const connectFor = (card) => run(`connect:${card.id}`,
    CONNECTOR_ACTIONS[card.id].connect);
  const disconnectFor = (card) => run(`disconnect:${card.id}`,
    CONNECTOR_ACTIONS[card.id].disconnect);
  const upgradeWrite = () => run('write', () => upgradeGoogleCalendarWrite());

  const renderCard = (card) => {
    const isGoogleCalendar = card.id === 'google_calendar';
    const isGmail = card.id === 'gmail';
    const cfg = CONNECTOR_ACTIONS[card.id] || null;
    const granted = card.status === 'connected' || card.status === 'expired';
    // Each connector shows its OWN read capability list — Calendar never shows
    // Mail's, and Mail never shows Calendar's (they share one provider view).
    const capabilitySummaries = isGoogleCalendar
      ? calendarReaderSummary(google)
      : isGmail ? gmailReaderSummary(google) : [];
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

        {capabilitySummaries.length ? (
          <ul className="capability-list">
            {capabilitySummaries.map((s) => (
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

        {isGmail ? (
          <p className="connection-notice secure">{GMAIL_READ_NOTICE}</p>
        ) : null}

        <div className="connection-actions">
          {card.connectable && cfg ? (
            <>
              {card.status === 'connected' ? (
                cfg.disconnect ? (
                  <button
                    type="button"
                    className="connection-btn secondary"
                    disabled={busy === `disconnect:${card.id}`}
                    onClick={() => disconnectFor(card)}
                  >
                    {DISCONNECT_LABEL}
                  </button>
                ) : (
                  <span className="connection-notice secure">{cfg.connectedNote}</span>
                )
              ) : (
                <button
                  type="button"
                  className="connection-btn primary"
                  disabled={busy === `connect:${card.id}`}
                  onClick={() => connectFor(card)}
                >
                  {card.status === 'expired' ? cfg.reconnect : cfg.label}
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
