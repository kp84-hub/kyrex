import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { disablePush, enablePush, notificationDeviceId, notificationRequest, pushSupported } from '../lib/notifications.js';
import '../Notifications.css';

export default function NotificationCenter({ onOpenConversation }) {
  const [open, setOpen] = useState(false), [items, setItems] = useState([]), [unread, setUnread] = useState(0);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [notice, setNotice] = useState('');
  const [settings, setSettings] = useState(null), [enabled, setEnabled] = useState(false), [deliveryError, setDeliveryError] = useState('');
  const [loading, setLoading] = useState(true);
  const panelId = useId(), trigger = useRef(null), container = useRef(null), panel = useRef(null);
  const onOpen = useRef(onOpenConversation); onOpen.current = onOpenConversation;
  const alive = useRef(true), sequence = useRef(0);
  const load = useCallback(async () => {
    const token = ++sequence.current;
    try {
      const data = await notificationRequest();
      if (!alive.current || token !== sequence.current) return;
      setItems(data.notifications || []); setUnread(data.unread || 0); setError('');
    } catch (failure) {
      if (!alive.current || token !== sequence.current) return;
      if (failure.status === 401) { setItems([]); setUnread(0); setEnabled(false); }
      setError(failure.message);
    } finally { if (alive.current && token === sequence.current) setLoading(false); }
  }, []);
  const openNotification = useCallback(async identity => {
    setBusy(true); setError(''); setNotice('');
    try {
      // Resolve again under the CURRENT authenticated account, not from a
      // stale row or an untrusted push deep link. A click grants no approval.
      const item = await notificationRequest('/' + encodeURIComponent(identity));
      await notificationRequest('/read', 'POST', { ids: [item.id] });
      if (!alive.current) return;
      if (item.conversation) { await onOpen.current?.(item.conversation); setOpen(false); }
      await load();
    } catch (failure) { if (alive.current) { setOpen(true); setError(failure.message); } }
    finally { if (alive.current) setBusy(false); }
  }, [load]);
  useEffect(() => {
    alive.current = true;
    let timer, cancelled = false;
    const poll = async () => {
      if (!cancelled && !document.hidden) await load();
      if (!cancelled) timer = setTimeout(poll, 20000);
    };
    poll();
    const refresh = () => { if (!document.hidden) load(); };
    const pushed = event => { if (event.data?.type === 'kyrex-notifications') refresh(); };
    window.addEventListener('focus', refresh); document.addEventListener('visibilitychange', refresh);
    navigator.serviceWorker?.addEventListener?.('message', pushed);
    return () => {
      cancelled = true; alive.current = false; ++sequence.current; clearTimeout(timer);
      window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh);
      navigator.serviceWorker?.removeEventListener?.('message', pushed);
    };
  }, [load]);
  useEffect(() => {
    const url = new URL(window.location.href), identity = url.searchParams.get('notification');
    if (!identity) return;
    url.searchParams.delete('notification'); window.history.replaceState({}, '', url);
    if (/^[a-f0-9]{32}$/.test(identity)) openNotification(identity);
  }, [openNotification]);
  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    panel.current?.querySelector('button:not(:disabled)')?.focus();
    const close = event => {
      if (event.type === 'keydown' && event.key !== 'Escape') return;
      if (event.type === 'pointerdown' && container.current?.contains(event.target)) return;
      setOpen(false);
      if (event.type === 'keydown') { event.preventDefault(); trigger.current?.focus(); }
    };
    const configure = async () => {
      try {
        const config = await notificationRequest('/settings');
        let status = { enabled: false };
        if (pushSupported()) status = await notificationRequest('/devices/' + notificationDeviceId());
        if (!cancelled) {
          setSettings(config); setEnabled(status.enabled && window.Notification.permission === 'granted');
          setDeliveryError(status.error || '');
        }
      } catch (failure) { if (!cancelled) setError(failure.message); }
    };
    configure();
    document.addEventListener('pointerdown', close); document.addEventListener('keydown', close);
    return () => { cancelled = true; document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', close); };
  }, [open]);
  const changePush = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      if (enabled) await disablePush(); else await enablePush(settings);
      if (alive.current) { setEnabled(!enabled); setDeliveryError(''); }
    } catch (failure) { if (alive.current) setError(failure.message); }
    finally { if (alive.current) setBusy(false); }
  };
  const markAll = async () => {
    setBusy(true); setError('');
    try { await notificationRequest('/read', 'POST', { ids: items.filter(item => !item.read).map(item => item.id) }); await load(); }
    catch (failure) { if (alive.current) setError(failure.message); }
    finally { if (alive.current) setBusy(false); }
  };
  const test = async () => {
    setBusy(true); setError(''); setNotice('');
    try { await notificationRequest('/devices/' + notificationDeviceId() + '/test', 'POST'); setNotice('Test queued. It should appear shortly.'); await load(); }
    catch (failure) { if (alive.current) setError(failure.message); }
    finally { if (alive.current) setBusy(false); }
  };
  return <div ref={container} className="notification-center">
    <button ref={trigger} type="button" className="notification-trigger" aria-label={`Notifications${unread ? `, ${unread} unread` : ''}`}
      aria-haspopup="dialog" aria-expanded={open} aria-controls={panelId} onClick={() => { setOpen(value => !value); load(); }}>
      <svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></svg>
      {unread > 0 && <span className="notification-count" aria-hidden="true">{unread > 99 ? '99+' : unread}</span>}
    </button>
    {open && <section ref={panel} id={panelId} className="notification-panel" role="dialog" aria-label="Notifications">
      <div className="notification-heading"><h2>Notifications</h2><button type="button" aria-label="Close notifications" onClick={() => { setOpen(false); trigger.current?.focus(); }}>×</button></div>
      <p>Task results, reviews, and automation alerts.</p>
      <div className="notification-device">
        <p>{enabled ? 'Device notifications are on for this browser.' : 'Enable device notifications to receive alerts when Chat is closed.'}</p>
        {pushSupported() ? <button type="button" disabled={busy || (!enabled && !settings?.available)} onClick={changePush}>
          {enabled ? 'Disable on this device' : 'Enable device notifications'}</button>
          : <p>Device notifications are unavailable in this browser. Your inbox still works.</p>}
        {settings && !settings.available && <p>{settings.message}</p>}
        {enabled && <button type="button" disabled={busy} onClick={test}>Send test notification</button>}
        {deliveryError && <p role="status">{deliveryError}</p>}
      </div>
      <div className="notification-heading"><span>{unread} unread</span><button type="button" disabled={busy || !items.some(item => !item.read)} onClick={markAll}>Mark shown as read</button></div>
      {loading && <p role="status">Loading notifications…</p>}
      {!loading && !items.length && !error && <p>No notifications yet.</p>}
      <ul>{items.map(item => <li key={item.id}><button type="button" className={`notification-item${item.read ? '' : ' unread'}`} disabled={busy} onClick={() => openNotification(item.id)}>
        <strong>{item.title}</strong><time dateTime={new Date(item.created * 1000).toISOString()}>{new Date(item.created * 1000).toLocaleString()}</time>
        <span>{item.kind === 'awaiting_approval' ? 'Open Chat to review this task.' : 'Open Chat to review the update.'}</span>
      </button></li>)}</ul>
      {notice && <p role="status">{notice}</p>}
      {error && <div role="alert"><p>{error}</p><button type="button" onClick={load}>Retry notifications</button></div>}
    </section>}
  </div>;
}
