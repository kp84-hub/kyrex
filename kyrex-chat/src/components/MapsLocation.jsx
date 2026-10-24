import React, { useEffect, useRef, useState } from 'react';
import { currentLocation } from '../lib/location.js';

// Only a fresh position for this explicit route is sent. No action/write is replayed.
export default function MapsLocation({ request, conversationId, onRouteFromHere }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pending = useRef(null);
  useEffect(() => {
    pending.current = null; setBusy(false); setError('');
    return () => pending.current?.abort();
  }, [conversationId, request]);
  const share = async () => {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError('');
    try {
      const fix = await currentLocation({ signal: controller.signal });
      if (controller.signal.aborted) return;
      if (fix.status !== 'available') {
        setError(fix.status === 'denied'
          ? 'Location is blocked. Allow it in your browser settings or type a starting address.'
          : 'Location unavailable. Try again or type a starting address.');
        return;
      }
      await onRouteFromHere(request, fix, conversationId);
    } catch {
      if (!controller.signal.aborted) setError('I couldn’t submit the route. Try again or open Google Maps.');
    } finally {
      if (!controller.signal.aborted) { pending.current = null; setBusy(false); }
    }
  };
  return <div className="maps-location" aria-live="polite">
    <button type="button" className="retry-btn" disabled={busy} onClick={share}>
      {busy ? 'Finding your location…' : 'Use current location'}
    </button>
    {error && <div className="message-error" role="status">{error}</div>}
  </div>;
}
