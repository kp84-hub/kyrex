import React, { useEffect, useState } from 'react';
import { getChatPrivacy, saveChatPrivacy } from '../lib/api.js';

export default function PrivacySettings() {
  const [settings, setSettings] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    getChatPrivacy().then((value) => { if (active) setSettings(value); })
      .catch((err) => { if (active) setError(err.message); });
    return () => { active = false; };
  }, []);
  const changeMemory = async (event) => {
    const share_saved_memory = event.target.checked;
    setSaving(true);
    setError('');
    try { setSettings(await saveChatPrivacy({ share_saved_memory })); }
    catch (err) { setError(err.message); }
    finally { setSaving(false); }
  };
  return (
    <section className="chat-privacy" aria-label="Chat privacy">
      <h3>Privacy</h3>
      <p>Kyrex filters recognizable passwords, API keys, and access tokens before model requests. Names, email text, calendar details, and messages needed for your task can still reach your selected provider.</p>
      <label>
        <input type="checkbox" checked={settings?.share_saved_memory ?? false}
          disabled={!settings || saving} onChange={changeMemory} />
        Share saved memories with models
      </label>
      <p>Turning this off stops adding saved memories to future requests. Memories stay saved; details already in this chat remain part of its history.</p>
      <p>Chat history is stored on your Kyrex server. Provider retention and training depend on your selected model.</p>
      {saving && <span role="status">Saving…</span>}
      {error && <div className="message-error" role="alert">{error}</div>}
    </section>
  );
}
