let temporaryDevice;
export function notificationDeviceId() {
  let saved;
  try { saved = localStorage.getItem('kyrex-notification-device'); } catch {}
  if (/^[a-f0-9]{32}$/.test(saved || '')) return saved;
  const identity = temporaryDevice ||= crypto.randomUUID().replaceAll('-', '');
  try { localStorage.setItem('kyrex-notification-device', identity); } catch {}
  return identity;
}

export async function notificationRequest(path = '', method = 'GET', body) {
  const response = await fetch('/api/notifications' + path, {
    method, credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000),
    ...(body ? { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } } : {}),
  });
  if (!response.ok) {
    const error = new Error('Notifications could not be loaded. Try again.'); error.status = response.status;
    try { error.message = (await response.json()).detail || error.message; } catch {}
    throw error;
  }
  return response.json();
}

export async function clearReviewedNotifications(ids = []) {
  window.dispatchEvent(new Event('kyrex-notifications-changed'));
  const valid = (Array.isArray(ids) ? ids : []).filter(id => /^[a-f0-9]{32}$/.test(id));
  if (!valid.length) return;
  try {
    const worker = await navigator.serviceWorker?.getRegistration('/');
    for (const id of valid) {
      const alerts = await worker?.getNotifications?.({ tag: 'kyrex-' + id });
      alerts?.forEach(alert => alert.close());
    }
  } catch { /* The saved inbox is already cleared, even without browser support. */ }
}

export function pushSupported() {
  return window.isSecureContext && 'Notification' in window && 'serviceWorker' in navigator && 'PushManager' in window;
}

function applicationKey(value) {
  return Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - value.length % 4) % 4)), char => char.charCodeAt(0));
}

async function registration() {
  await navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' });
  let timer;
  try {
    return await Promise.race([navigator.serviceWorker.ready,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Notification setup took too long. Reload Chat and try again.')), 15000); })]);
  } finally { clearTimeout(timer); }
}

export async function enablePush(settings) {
  if (!pushSupported()) throw new Error('Device notifications are unavailable in this browser. Your inbox still works.');
  if (!settings?.available) throw new Error(settings?.message || 'Device notifications are unavailable on this server.');
  // Keep the permission prompt directly inside the user's click, before any
  // network/worker await; browsers require that deliberate gesture.
  const permission = await window.Notification.requestPermission();
  if (permission !== 'granted') throw new Error('Notifications were not allowed. You can change this in your browser’s site settings.');
  const worker = await registration();
  let subscription = await worker.pushManager.getSubscription(), created = false;
  const key = applicationKey(settings.public_key);
  const oldKey = subscription?.options?.applicationServerKey;
  if (oldKey && String(new Uint8Array(oldKey)) !== String(key)) {
    await subscription.unsubscribe(); subscription = null;
  }
  if (!subscription) {
    subscription = await worker.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }); created = true;
  }
  try {
    await notificationRequest('/devices/' + notificationDeviceId(), 'POST', { subscription: subscription.toJSON() });
  } catch (error) {
    if (created) try { await subscription.unsubscribe(); } catch {}
    throw error;
  }
}

export async function disablePush() {
  // Stop server delivery first, even when the OS/browser cleanup is unavailable.
  await notificationRequest('/devices/' + notificationDeviceId(), 'DELETE');
  try {
    const worker = await navigator.serviceWorker?.getRegistration('/');
    await (await worker?.pushManager.getSubscription())?.unsubscribe();
  } catch {}
}
