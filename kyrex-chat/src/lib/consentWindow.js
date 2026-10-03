// Reserve the window during the tap, before the async OAuth start request.
// Mobile browsers otherwise commonly block window.open after the request.
export function reserveConsentWindow() {
  try {
    const popup = window.open('about:blank', '_blank');
    if (popup) popup.opener = null;
    return popup;
  } catch {
    return null;
  }
}

export function consentUrl(raw) {
  if (typeof raw !== 'string' || !raw) throw new Error('Connection did not return a sign-in page.');
  const url = new URL(raw, window.location.origin);
  const localSetup = url.origin === window.location.origin && url.pathname === '/api/connections/messages/setup';
  const google = url.protocol === 'https:' && url.hostname === 'accounts.google.com' && !url.port && url.pathname.startsWith('/o/oauth2/');
  if ((!localSetup && !google) || url.username || url.password) throw new Error('Connection returned an unsupported sign-in page.');
  return url.href;
}

export function navigateConsentWindow(popup, url) {
  try {
    if (!popup || popup.closed) return false;
    popup.location.replace(url);
    return true;
  } catch {
    return false;
  }
}

export function closeConsentWindow(popup) {
  try { popup?.close(); } catch { /* User may already have closed it. */ }
}
