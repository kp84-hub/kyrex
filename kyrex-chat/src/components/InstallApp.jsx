import React, { useEffect, useState } from 'react';

const isInstalled = () => window.matchMedia?.('(display-mode: standalone)').matches
  || window.navigator.standalone === true;

export default function InstallApp() {
  const [installed, setInstalled] = useState(isInstalled);
  const [prompt, setPrompt] = useState(null);
  const [help, setHelp] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const ready = (event) => { event.preventDefault(); setPrompt(event); setHelp(false); };
    const done = () => { setInstalled(true); setPrompt(null); setHelp(false); };
    const media = window.matchMedia?.('(display-mode: standalone)');
    const changed = () => setInstalled(isInstalled());
    window.addEventListener('beforeinstallprompt', ready);
    window.addEventListener('appinstalled', done);
    media?.addEventListener?.('change', changed);
    return () => {
      window.removeEventListener('beforeinstallprompt', ready);
      window.removeEventListener('appinstalled', done);
      media?.removeEventListener?.('change', changed);
    };
  }, []);

  if (installed) return null;
  const install = async () => {
    if (!prompt) { setHelp((value) => !value); return; }
    setBusy(true);
    try {
      await prompt.prompt();
      await prompt.userChoice;
    } catch {
      setHelp(true);
    } finally {
      setPrompt(null);
      setBusy(false);
    }
  };
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
