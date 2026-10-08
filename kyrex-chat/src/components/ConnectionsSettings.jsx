import React, { useEffect, useMemo, useRef, useState } from 'react';
import MessagesStatus from './MessagesStatus.jsx';
import { reserveConsentWindow, consentUrl, navigateConsentWindow, closeConsentWindow } from '../lib/consentWindow.js';
import {
  connectOura, pairSamsungHealth, disconnectFitness,
  connectGoogle, disconnectGoogle, fetchConnections, upgradeGoogleCalendarWrite,
  upgradeGoogleGmailRead, pairMessages, disconnectMessages, disconnectGitHub, connectGitHub, manageGitHub,
} from '../lib/api.js';
import {
  CONNECT_GMAIL_LABEL, CONNECT_LABEL, DISCONNECT_LABEL, GMAIL_READ_NOTICE,
  RECONNECT_GMAIL_LABEL, UNAVAILABLE_NOTICE,
  WRITE_ENABLED_NOTICE, calendarReaderSummary, gmailReaderSummary,
  joinCapabilities, safeText, statusClassOf, statusLabelOf,
} from '../lib/connections.js';
import {
  COMING_SOON_LABEL, READ_ONLY_BADGE, READ_WRITE_BADGE, SECTION_AVAILABLE,
  SECTION_CONNECTED, WRITE_UPGRADE_NOTICE, buildHubModel,
} from '../lib/connectorRegistry.js';

function ConnectorIcon({ id, fallback }) {
  if (id === 'github') {
    return <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#151a20" d="M12 .5a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2.3c-3.3.7-4-1.4-4-1.4-.5-1.4-1.3-1.7-1.3-1.7-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.9 1.3 3.6 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.6.1-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0c2.3-1.5 3.3-1.2 3.3-1.2.6 1.6.2 2.9.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A12 12 0 0 0 12 .5Z" /></svg>;
  }
  if (id === 'google_calendar') {
    return <svg viewBox="0 0 40 40" fill="none" aria-hidden="true">
      <rect x="5" y="6" width="30" height="29" rx="5" fill="#4285f4" />
      <path d="M5 11a5 5 0 0 1 5-5h20a5 5 0 0 1 5 5v3H5z" fill="#1967d2" />
      <path d="M5 28h10v7h-5a5 5 0 0 1-5-5z" fill="#34a853" />
      <path d="M28 25h7v5a5 5 0 0 1-5 5h-5z" fill="#fbbc04" />
      <path d="M13 4v7m14-7v7" stroke="#fff" strokeWidth="3" strokeLinecap="round" />
      <text x="20" y="28" textAnchor="middle" fill="white" fontSize="15" fontWeight="700" fontFamily="Arial, sans-serif">31</text>
    </svg>;
  }
  if (id === 'messages') {
    return <svg viewBox="0 0 40 40" fill="none" aria-hidden="true"><path d="M32 19c0 8-6 13-14 13l-10 3 2-9C1 10 13 4 23 7c6 2 9 6 9 12Z" stroke="#151a20" strokeWidth="2.5" strokeLinejoin="round"/><path d="M13 16h13m-13 6h9" stroke="#151a20" strokeWidth="2.5" strokeLinecap="round"/></svg>;
  }
  if (id === 'gmail') {
    return <svg viewBox="0 0 40 40" fill="none" aria-hidden="true">
      <path d="M7 30V12l13 10 13-10v18" stroke="#ea4335" strokeWidth="5" strokeLinejoin="round" />
      <path d="M7 19v11" stroke="#4285f4" strokeWidth="5" />
      <path d="M33 19v11" stroke="#34a853" strokeWidth="5" />
      <path d="M7 12v7m26-7v7" stroke="#fbbc04" strokeWidth="5" />
    </svg>;
  }
  return <span aria-hidden="true">{fallback}</span>;
}

function Chevron({ back = false }) {
  return <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d={back ? 'm15 5-7 7 7 7' : 'm9 5 7 7-7 7'}
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
}

// Compact connector rows keep account permissions and management in details.
export default function ConnectionsSettings({ onClose }) {
  const [pairing, setPairing] = useState(null);
  const [healthPairing, setHealthPairing] = useState(null);
  const [views, setViews] = useState([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [available, setAvailable] = useState(true);
  const [pending, setPending] = useState(null);
  const [messagesOpen, setMessagesOpen] = useState(false);
  const consentPopup = useRef(null);

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
  useEffect(() => { refresh(); const onFocus = () => refresh(); window.addEventListener('focus', onFocus); return () => window.removeEventListener('focus', onFocus); }, []);

  // Observe the provider's actual result; opening a window is not connection.
  useEffect(() => {
    if (!pending) return;
    const cards = buildHubModel(views).connected;
    const card = cards.find(c => c.id === pending.id);
    if (card?.status === 'connected' && (!pending.write || card.hasWriteScope)) {
      setPending(null);
      if (pending.id === 'messages') setPairing(null);
    }
  }, [pending, views]);
  useEffect(() => {
    if (!pending) return;
    const interval = setInterval(() => {
      if (Date.now() >= pending.deadline) {
        setPending(null);
        setPairing(null);
        setError('Connection was not completed. Tap Connect to try again.');
      } else refresh();
    }, 2500);
    return () => clearInterval(interval);
  }, [pending]);

  const hub = useMemo(() => buildHubModel(views, query), [views, query]);
  const google = views.find((v) => v && v.provider === 'google') || null;

  const run = async (kind, fn) => {
    const needsConsent = (kind.startsWith('connect:') && !['connect:samsung_health', 'connect:messages'].includes(kind)) || kind === 'write';
    // This runs synchronously in the owner's tap handler.
    const popup = needsConsent ? reserveConsentWindow() : null;
    if (needsConsent) consentPopup.current = popup;
    setBusy(kind);
    setError('');
    try {
      const result = await fn();
      if (needsConsent) {
        const url = consentUrl(result?.authorization_url);
        const opened = navigateConsentWindow(popup, url);
        setPending({ id: kind === 'write' ? 'google_calendar' : kind.split(':')[1],
          write: kind === 'write', url, opened, deadline: Date.now() + 5 * 60 * 1000 });
      }
      if (kind === 'connect:samsung_health') setHealthPairing(result);
      if (kind === 'disconnect:samsung_health') setHealthPairing(null);
      if (kind === 'connect:messages') {
        if (!result?.pairing_code || !Number.isFinite(result.expires_at)) throw new Error('Messages phone pairing is unavailable. Update the Kyrex server and try again.');
        setPairing(result);
        setPending({ id: 'messages', phone: true, deadline: result.expires_at * 1000 });
      }
      if (kind === 'disconnect:messages') { setPairing(null); setPending(null); }
      if (!kind.startsWith('retry:')) await refresh();
    } catch (e) {
      closeConsentWindow(popup);
      if (e && e.status === 503 && !kind.endsWith(':messages') && !kind.endsWith(':github') && !kind.endsWith(':oura') && !kind.endsWith(':samsung_health')) setAvailable(false);
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
    oura: { connect: () => connectOura(), disconnect: () => disconnectFitness('oura'), label: 'Connect Oura', reconnect: 'Reconnect Oura' },
    samsung_health: { connect: () => pairSamsungHealth(), disconnect: () => disconnectFitness('samsung_health'), label: 'Pair phone', reconnect: 'Pair phone again' },
    github: { connect: () => connectGitHub(), disconnect: () => disconnectGitHub(), label: 'Connect GitHub', reconnect: 'Reconnect GitHub' },
    google_calendar: {
      connect: () => connectGoogle(),
      disconnect: () => disconnectGoogle(),
      label: CONNECT_LABEL,
      reconnect: 'Reconnect Google Calendar',
      connectedNote: '',
    },
    messages: {
      connect: () => pairMessages(), disconnect: () => disconnectMessages(),
      label: 'Connect Messages', reconnect: 'Reconnect Messages', connectedNote: '',
    },
    gmail: {
      connect: () => upgradeGoogleGmailRead(),
      disconnect: null,
      label: CONNECT_GMAIL_LABEL,
      reconnect: RECONNECT_GMAIL_LABEL,
      connectedNote: 'Gmail read is enabled (read-only).',
    },
  };

  const connectFor = (card) => {
    if (card.status === 'unavailable') return run(`retry:${card.id}`, refresh);
    return run(`connect:${card.id}`, CONNECTOR_ACTIONS[card.id].connect);
  };
  const disconnectFor = (card) => run(`disconnect:${card.id}`,
    CONNECTOR_ACTIONS[card.id].disconnect);
  const upgradeWrite = () => run('write', () => upgradeGoogleCalendarWrite());

  const renderCard = (card) => {
    const isPhoneMessages = card.id === 'messages' && views.some(v => v?.provider === 'device_messages' && v.mode === 'android_companion');
    const isGoogleCalendar = card.id === 'google_calendar';
    const isGmail = card.id === 'gmail';
    const cfg = CONNECTOR_ACTIONS[card.id] || null;
    const granted = card.status === 'connected' || card.status === 'expired' || card.status === 'unavailable';
    // Each connector shows its OWN read capability list — Calendar never shows
    // Mail's, and Mail never shows Calendar's (they share one provider view).
    const capabilitySummaries = isGoogleCalendar
      ? calendarReaderSummary(google)
      : isGmail ? gmailReaderSummary(google) : [];
    return (
      <details
        key={card.id}
        className={`connection-card connector-card${card.connectable ? '' : ' planned'}`}
        aria-label={`${card.name} connector`}
        open={card.id === 'messages' ? messagesOpen : undefined}
        onToggle={card.id === 'messages' ? (event) => setMessagesOpen(event.currentTarget.open) : undefined}
      >
        <summary className="connector-row">
          <span className={`connector-icon service-${card.id}`}>
            <ConnectorIcon id={card.id} fallback={card.icon} />
          </span>
          <span className="connector-identity">
            <strong>{card.name}</strong>
            {card.subtitle ? <span className="connector-category">{card.subtitle}</span> : null}
            {card.status === 'expired' ? <span className="connector-category">Connection expired</span> : null}
            {card.status === 'unavailable' ? <span className="connector-category">Temporarily unavailable</span> : null}
          </span>
          {card.connectable && cfg && card.status !== 'connected' ? (
            <button
              type="button"
              className="connector-connect"
              aria-label={card.status === 'unavailable' ? `Retry ${card.name}` : card.status === 'expired' ? cfg.reconnect : cfg.label}
              disabled={Boolean(busy) || Boolean(pending)}
              onClick={(event) => {
                event.preventDefault();
                connectFor(card);
              }}
            >
              {busy === `retry:${card.id}` ? 'Retrying…' : busy === `connect:${card.id}` ? 'Connecting…' : card.status === 'unavailable' ? 'Retry' : card.status === 'expired' ? 'Reconnect' : 'Connect'}
            </button>
          ) : !card.connectable ? (
            <button type="button" className="connector-coming-soon" disabled>{COMING_SOON_LABEL}</button>
          ) : (
            <span className="connector-chevron"><Chevron /></span>
          )}
        </summary>
        <div className="connector-detail-panel">
          <span className={`connection-status ${statusClassOf(card.status)}`}>
            {isPhoneMessages
              ? (card.status === 'connected' ? 'Saved texts available' : card.paired ? 'No saved texts yet' : 'Not linked')
              : card.connectable ? statusLabelOf(card.status) : COMING_SOON_LABEL}
          </span>

          <p className="connection-detail">{card.description}</p>
          {card.id === 'oura' ? <p>Authorize the data you want to share. Oura access is read-only. Create a Workout Bot in Bots to use your readings.</p> : null}
          {card.id === 'samsung_health' ? <div>
            <p>On your phone, allow Samsung Health to sync with Health Connect, then open the Kyrex Health companion app.</p>
            {healthPairing && healthPairing.expires_at * 1000 > Date.now() ? <p>Enter this pairing code in the companion app: <strong>{healthPairing.pairing_code}</strong>. It expires in 10 minutes.</p> : null}
            {card.syncedAt ? <p>Last synced: {new Date(card.syncedAt * 1000).toLocaleString()}</p> : <p>No phone data has synced yet.</p>}
          </div> : null}
          {card.id === 'github' ? <p>Sign in on GitHub and choose which repositories Kyrex can read.</p> : null}

          {card.id === 'messages' && card.connectable ? (
            <div className="messages-setup">
              {isPhoneMessages && messagesOpen ? <MessagesStatus /> : null}
              <p>Open the Kyrex Messages companion on your phone. Connect Google Messages there, then link your Kyrex account.</p>
              <p>{card.sendEnabled ? "Confirmed sending from Chat is enabled on your phone." : "To send from Chat, enable Allow sends confirmed in Kyrex Chat in the updated companion."}</p>
              <p>With your confirmation, the phone uploads up to 100 SMS/RCS text messages from 10 recent conversations. Keep the companion open for new-message sync, or tap Sync now there.</p>
              {card.syncedAt ? <p>Last synced: {new Date(card.syncedAt * 1000).toLocaleString()}</p> : <p>No phone data has synced yet.</p>}
            </div>
          ) : null}

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
            {card.id === 'github' && card.status === 'connected' ? <button type="button" className="connection-btn secondary"
              disabled={Boolean(busy)} onClick={() => run('connect:github', () => manageGitHub())}>Manage repositories</button> : null}
            {card.connectable && cfg ? (
              <>
                {card.status === 'connected' || card.paired || (card.id === 'messages' && pairing) ? (
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
                ) : null}
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
                    disabled={Boolean(busy) || Boolean(pending)}
                    onClick={upgradeWrite}
                  >
                    {card.writeUpgrade.label}
                  </button>
                </div>
              )}
            </div>
          ) : null}
        </div>
      </details>
    );
  };

  return (
    <section
      className="provider-settings connections-hub"
      aria-label="Connections"
    >
      <div className="settings-heading connectors-heading">
        {onClose && (
          <button type="button" className="connectors-back" aria-label="Close connectors" onClick={onClose}>
            <Chevron back />
          </button>
        )}
        <h2>Connectors</h2>
      </div>

      <div className="connectors-search-wrap">
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="10.5" cy="10.5" r="7" stroke="currentColor" strokeWidth="1.8" />
          <path d="m16 16 5 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
        <input
          type="search"
          className="connections-search"
          placeholder="Search connectors"
          aria-label="Search connectors"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {pending ? <div className="connection-notice" role="status">
        {pending.phone ? <>Open the companion’s Link Kyrex account section. Server: <strong>{window.location.origin}</strong>. Pairing code (expires in 15 minutes): <strong style={{ overflowWrap: 'anywhere' }}>{pairing?.pairing_code}</strong>. Confirm Link and sync on your phone.{' '}<button type="button" className="connection-btn secondary" onClick={async () => {
          try { await navigator.clipboard.writeText(pairing.pairing_code); }
          catch { setError('Could not copy automatically. Select and copy the pairing code above.'); }
        }}>Copy pairing code</button></> : <>
        {pending.opened ? 'Finish connecting in the sign-in window.' : 'Open the sign-in page to finish connecting.'}
        {' '}<a href={pending.url} target="_blank" rel="noreferrer">Continue connecting</a></>}
        {' '}<button type="button" className="connection-btn secondary" onClick={() => {
          closeConsentWindow(consentPopup.current);
          setPending(null); setPairing(null);
        }}>Close setup</button>
      </div> : null}

      {!available ? (
        <div className="message-error">{UNAVAILABLE_NOTICE}</div>
      ) : (
        <>
          {hub.connected.length ? (
            <section className="connections-section" aria-label={SECTION_CONNECTED}>
              <h3 className="connections-section-title">{SECTION_CONNECTED}</h3>
              <div className="connections-list">{hub.connected.map(renderCard)}</div>
            </section>
          ) : null}

          {hub.available.length ? (
            <section className="connections-section" aria-label={SECTION_AVAILABLE}>
              <h3 className="connections-section-title">{SECTION_AVAILABLE}</h3>
              <div className="connections-list">{hub.available.map(renderCard)}</div>
            </section>
          ) : null}

          {hub.isEmpty ? (
            <div className="connections-empty">
              No connections match “{safeText(query)}”.
            </div>
          ) : null}

          {error ? <div className="message-error">{error}</div> : null}
        </>
      )}
    </section>
  );
}
