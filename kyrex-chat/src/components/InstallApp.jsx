import React from 'react';

export default function InstallApp({ state }) {
  const { installed, help, busy, install } = state;
  if (installed) return null;
  const appleMobile = /iPhone|iPad|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  return <div className="install-app">
    <button type="button" className="sidebar-settings install-app-button"
      onClick={install} disabled={busy} aria-describedby={help ? 'install-help' : undefined}>
      {busy ? 'Opening installer…' : 'Install Kyrex Chat'}
    </button>
    {help && <p id="install-help" className="install-help" role="status">
      {appleMobile
        ? 'In Safari, tap Share, then Add to Home Screen.'
        : 'In Chrome or Edge, open the browser menu and choose Install app or Add to Home screen. If it is missing, keep the page open briefly and try again.'}
    </p>}
  </div>;
}
