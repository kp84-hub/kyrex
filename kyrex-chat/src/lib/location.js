// One device fix for an explicitly location-related turn. Never track or store it.
export function isLocationRequest(text = '') {
  return /\b(?:how far(?!\s+(?:along|into|back|ahead)\b)|how long.*(?:drive|walk|travel|get to)|(?:distance|travel time) (?:to|from|between)|directions? to|navigate to|route to|take me to|near me|nearby|closest|nearest|current location)\b|^\s*where am i[?.!]*\s*$/i.test(text);
}

export function currentLocation({ signal } = {}) {
  if (!globalThis.navigator?.geolocation) return Promise.resolve({ status: 'unavailable' });
  return new Promise(resolve => {
    let finished = false;
    const finish = value => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      resolve(value);
    };
    const abort = () => finish({ status: 'unavailable' });
    // Browser timeouts may exclude time spent waiting for permission.
    const timer = setTimeout(abort, 10000);
    if (signal?.aborted) { abort(); return; }
    signal?.addEventListener('abort', abort, { once: true });
    try {
      navigator.geolocation.getCurrentPosition(position => finish({
        status: 'available', latitude: position.coords.latitude, longitude: position.coords.longitude,
        accuracy_meters: position.coords.accuracy, captured_at: position.timestamp,
      }), error => finish({ status: error.code === 1 ? 'denied' : 'unavailable' }),
      { enableHighAccuracy: true, maximumAge: 60000, timeout: 8000 });
    } catch { finish({ status: 'unavailable' }); }
  });
}

export async function locationForMessage(text, explicit, options = {}) {
  if (!isLocationRequest(text)) return undefined;
  if (explicit && explicit.status !== 'available') return { status: explicit.status };
  if (explicit?.status === 'available' && Date.now() - explicit.captured_at <= 60000) return explicit;
  // Only the explicit composer control can trigger a new permission prompt.
  // Already-granted permission also works for voice submissions.
  try {
    const permission = await navigator.permissions?.query({ name: 'geolocation' });
    if (permission?.state === 'granted') return currentLocation(options);
    return { status: permission?.state === 'denied' ? 'denied' : 'not_shared' };
  } catch { return { status: 'not_shared' }; }
}
