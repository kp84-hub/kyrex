import React, { useEffect, useRef, useState } from 'react';
import { accountRequest, cancelChat, streamChat } from '../lib/api.js';
import ReactMarkdown from 'react-markdown';

export default function ManagedChat({ account }) {
  const [conversations, setConversations] = useState([]), [active, setActive] = useState('');
  const [messages, setMessages] = useState([]), [text, setText] = useState(''), [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const work = useRef(null), running = useRef(false);
  const refresh = async () => setConversations((await accountRequest('/api/conversations')).conversations || []);
  useEffect(() => { refresh().catch(e => setError(e.message)); return () => { work.current?.cancel(); }; }, []);
  const select = async id => {
    if (running.current) return;
    setError('');
    try { const value = await accountRequest('/api/conversations/' + encodeURIComponent(id)); setActive(id); setMessages(value.messages || []); }
    catch (e) { setError(e.message); }
  };
  const send = async e => {
    e.preventDefault();
    if (running.current || !text.trim()) return;
    const content = text.trim(), requestId = crypto.randomUUID();
    running.current = true; setBusy(true); setError(''); setText('');
    const initial = [...messages, { role: 'user', content }];
    setMessages(initial);
    let answer = '';
    try {
      const current = streamChat(active, content, requestId);
      work.current = { ...current, requestId };
      for await (const frame of current.stream) {
        if (frame.type === 'conversation') setActive(frame.conversation_id);
        if (frame.type === 'delta') { answer += frame.content; setMessages([...initial, { role: 'assistant', content: answer }]); }
        if (frame.type === 'done') setMessages([...initial, { role: 'assistant', content: frame.content }]);
        if (frame.type === 'error') throw new Error(frame.message);
      }
      await refresh();
    } catch (e) { if (e.name !== 'AbortError') setError(e.message); }
    finally { running.current = false; work.current = null; setBusy(false); }
  };
  return <main className="managed-chat" aria-label="Managed learning chat">
    <header><strong>Kyrex · {account.name}</strong><a href="/auth/logout">Sign out</a></header>
    <p className="managed-notice">Your parent manages this account and can review your chats. This preview supports text learning and coding explanations. It cannot access email, calendars, files, or run code.</p>
    <div className="managed-history"><button className="onboarding-secondary" disabled={busy} onClick={() => { setActive(''); setMessages([]); setError(''); }}>New chat</button>
      <select aria-label="Conversation history" disabled={busy} value={active} onChange={e => select(e.target.value)}><option value="">Choose a conversation</option>
        {conversations.map(c => <option key={c.conversation_id} value={c.conversation_id}>{c.title}</option>)}</select></div>
    <section className="managed-messages" aria-live="polite">
      {!messages.length && <h1>What would you like to learn or make?</h1>}
      {messages.map((m, i) => <article className={`managed-message ${m.role}`} key={m.id || i}><strong>{m.role === 'user' ? 'You' : 'Kyrex'}</strong><ReactMarkdown>{m.content}</ReactMarkdown></article>)}
    </section>
    {error && <p role="alert" className="message-error">{error}</p>}
    <form onSubmit={send}><label className="sr-only" htmlFor="learning-message">Message Kyrex</label><textarea id="learning-message" required disabled={busy} maxLength={8000} value={text} onChange={e => setText(e.target.value)} placeholder="Ask a question or describe an idea…" />
      <button className="send-btn" disabled={busy || !text.trim()}>Send</button>
      {busy && <button type="button" className="onboarding-secondary" onClick={() => { const current = work.current; if (current) cancelChat(current.requestId).catch(e => setError(e.message)); }}>Stop</button>}
    </form>
  </main>;
}
