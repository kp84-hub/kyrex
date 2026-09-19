import React, { useEffect, useState } from 'react';
import { connectGoogle, disconnectGoogle, fetchConnections } from '../lib/api.js';
import {
  CONNECT_LABEL, DISCONNECT_LABEL, READ_ONLY_NOTICE, SECRET_NOTICE,
  UNAVAILABLE_NOTICE, capabilityLines, primaryActionOf, safeText, statusLabelOf,
  statusOf,
} from '../lib/connections.js';

// Settings -> Connections (Google Calendar, read-only). The backend is the
// source of truth for status/expiry; this component never re-derives expiry
// and NEVER renders a token, client secret, or authorization code — every
// string that reaches the DOM goes through safeText().
export default function ConnectionsSettings({ onClose }) {
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);
  const [connection, setConnection] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await fetchConnections();
      const list = (data && data.connectors) || [];
      setConnection(list[0] || null);
      setUnavailable(false);
    } catch (e) {
      if (e && e.status === 503) setUnavailable(true);
      else setError(safeText(e && e.message));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const connect = async () => {
    setBusy(true);
    setError('');
    try {
      const started = await connectGoogle();
      // The response carries ONLY the authorization URL (with the single-use,
      // owner-bound state) — never a token or secret.
      if (started && started.authorization_url) {
        window.location.assign(started.authorization_url);
      }
    } catch (e) {
      if (e && e.status === 503) setUnavailable(true);
      else setError(safeText(e && e.message));
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setError('');
    try {
      await disconnectGoogle();
      await refresh();
    } catch (e) {
      setError(safeText(e && e.message));
    } finally {
      setBusy(false);
    }
  };

  const status = statusOf(connection);
  const action = primaryActionOf(status);
  const caps = capabilityLines(connection);

  return (
    <section className="provider-settings" aria-label="Connections">
      <div className="settings-heading">
        <div>
          <h2>Connections</h2>
          <p>
            Connect Google Calendar (read-only) so a Calendar Reader Bot can
            answer calendar: today, calendar: tomorrow, and calendar: week.
          </p>
        </div>
        {onClose && (
          <button type="button" className="settings-close" onClick={onClose}>Close</button>
        )}
      </div>
      {unavailable ? (
        <div className="message-error">{UNAVAILABLE_NOTICE}</div>
      ) : (
        <>
          <div className="provider-list">
            <div className="provider-row">
              <div>
                <strong>Google Calendar</strong>
                <span>{statusLabelOf(status)}</span>
                <span className="provider-secret">{READ_ONLY_NOTICE}</span>
              </div>
              {action === 'disconnect' ? (
                <button
                  type="button"
                  className="conversation-delete visible"
                  disabled={busy}
                  onClick={disconnect}
                >
                  {DISCONNECT_LABEL}
                </button>
              ) : (
                <button
                  type="button"
                  className="send-btn"
                  disabled={busy || loading}
                  onClick={connect}
                >
                  {action === 'reconnect' ? 'Reconnect Google Calendar' : CONNECT_LABEL}
                </button>
              )}
            </div>
          </div>
          {caps.unsupported.length > 0 && (
            <p className="provider-secret">
              Not available in this phase: {caps.unsupported.join(', ')}.
            </p>
          )}
          <p className="provider-secret">{SECRET_NOTICE}</p>
        </>
      )}
      {error && <div className="message-error">{error}</div>}
    </section>
  );
}
