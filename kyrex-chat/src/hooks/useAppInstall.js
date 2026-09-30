import { useEffect, useState } from 'react';

const isInstalled = () => window.matchMedia?.('(display-mode: standalone)').matches
  || window.navigator.standalone === true;

export function useAppInstall() {
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
  return { installed, help, busy, install };
}
