import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import AccountGate from './components/AccountGate.jsx';
import AndroidHealthSetup from './components/AndroidHealthSetup.jsx';
import { readAndroidHealthSetup } from './lib/androidHealth.js';
import './App.css';
import { isKbotSurface } from './lib/kbotSurface.js';
if (isKbotSurface()) document.documentElement.dataset.kbot = 'true';
import './BotRights.css';

// Keep development unregistered; production always checks for the latest worker.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' })
      .catch((error) => console.warn('Kyrex app installation setup:', error));
  }, { once: true });
}

const androidHealthSetup = readAndroidHealthSetup(window.location, window.sessionStorage);
createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    {androidHealthSetup ? <AndroidHealthSetup request={androidHealthSetup} /> : <AccountGate><App /></AccountGate>}
  </React.StrictMode>
);
