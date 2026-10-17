const proof = /^[A-Za-z0-9_-]{43,128}$/;
const pendingKey = 'kyrex.android.health.setup.v1';

export function readAndroidHealthSetup(location, storage) {
  const params = new URLSearchParams(location.search);
  let value = null;
  try {
    if (params.get('android_health') === '1') {
      value = { challenge: params.get('challenge'), state: params.get('state') };
      if (typeof value.challenge !== 'string' || value.challenge.length !== 43 ||
          !proof.test(value.challenge) || typeof value.state !== 'string' || !proof.test(value.state)) {
        storage.removeItem(pendingKey);
        return { invalid: true };
      }
      value.expires = Date.now() + 10 * 60 * 1000;
      storage.setItem(pendingKey, JSON.stringify(value));
    } else {
      value = JSON.parse(storage.getItem(pendingKey) || 'null');
      if (!value || value.expires < Date.now()) {
        storage.removeItem(pendingKey);
        return null;
      }
    }
    return value;
  } catch { return params.get('android_health') === '1' ? { invalid: true } : null; }
}

export function clearAndroidHealthSetup(storage) { storage.removeItem(pendingKey); }

export function androidHealthIntent(result = null) {
  if (!result) return 'intent://health/start#Intent;scheme=kyrex-chat;package=com.kyrex.chat;end';
  if (!proof.test(result.code || '') || !proof.test(result.state || '')) throw new Error('Invalid phone connection.');
  return `intent://health/complete?code=${encodeURIComponent(result.code)}&state=${encodeURIComponent(result.state)}#Intent;scheme=kyrex-chat;package=com.kyrex.chat;end`;
}
