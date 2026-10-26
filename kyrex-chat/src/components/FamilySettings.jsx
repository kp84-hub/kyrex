import React, { useEffect, useState } from 'react';
import { accountRequest } from '../lib/api.js';

function Member({ member, profiles, onRefresh, act, busy, onReview }) {
  const [profile, setProfile] = useState(member.provider_profile_id || ''), [model, setModel] = useState(member.model || '');
  const [limit, setLimit] = useState(member.daily_messages || 25), [pair, setPair] = useState('');
  const [confirm, setConfirm] = useState('');
  const base = '/api/account/family/' + encodeURIComponent(member.owner);
  return <article className="family-member">
    <h4>{member.name} · {member.active ? 'Active' : 'Paused'}</h4>
    <p>Text learning only. No connected accounts, Bots, uploads, or code execution. You can review these chats.</p>
    <form onSubmit={e => { e.preventDefault(); act(async () => {
      await accountRequest(base, 'PATCH', { provider_profile_id: profile, model, daily_messages: Number(limit) }); await onRefresh();
    }); }}>
      <label>Provider you fund<select required disabled={busy} value={profile} onChange={e => setProfile(e.target.value)}><option value="">Choose your saved provider</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      <label>Learning model<input required disabled={busy} maxLength={200} value={model} onChange={e => setModel(e.target.value)} /></label>
      <label>Daily message limit<input required disabled={busy} type="number" min="1" max="100" value={limit} onChange={e => setLimit(e.target.value)} /></label>
      <p>The server operator must enable this exact provider endpoint and model for managed use. Your API key stays in your account.</p>
      <button className="onboarding-secondary" disabled={busy || !profile || !model}>Save learning settings</button>
    </form>
    <div className="family-actions">
      <button className="onboarding-secondary" disabled={busy} onClick={() => act(async () => { await accountRequest(base, 'PATCH', { active: !member.active }); await onRefresh(); })}>{member.active ? 'Pause account' : 'Resume account'}</button>
      <button className="onboarding-secondary" disabled={busy || !member.active} onClick={() => act(async () => { setPair((await accountRequest(base + '/pair', 'POST')).pairing_code); })}>Pair a device</button>
      <button className="onboarding-secondary" disabled={busy} onClick={() => act(async () => { await accountRequest(base + '/revoke-devices', 'POST'); setPair(''); })}>Sign out all paired devices</button>
      <button className="onboarding-secondary" disabled={busy} onClick={() => onReview(member)}>Review chats</button>
    </div>
    {pair && <div role="status"><p>Open this link on the managed device within 10 minutes. It works once.</p>
      <input aria-label={`Pairing link for ${member.name}`} readOnly value={window.location.origin + '/#pair=' + encodeURIComponent(pair)} onFocus={e => e.target.select()} />
      <p>Or enter this pairing code on its welcome screen:</p><code className="pairing-code">{pair}</code></div>}
    <details><summary>Delete managed account data</summary><p>This removes its chat history and revokes its devices.</p>
      <label>Type {member.name} to confirm<input value={confirm} onChange={e => setConfirm(e.target.value)} autoComplete="off" /></label>
      <button className="onboarding-secondary" disabled={busy || confirm !== member.name} onClick={() => act(async () => { await accountRequest(base, 'DELETE', { confirm_name: confirm }); await onRefresh(); })}>Delete account and chats</button>
    </details>
  </article>;
}

export default function FamilySettings({ profiles = [] }) {
  const [options, setOptions] = useState(null), [account, setAccount] = useState(null), [members, setMembers] = useState([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [name, setName] = useState(''), [age, setAge] = useState('under13'), [consent, setConsent] = useState(false);
  const [recipient, setRecipient] = useState(''), [email, setEmail] = useState(''), [challenge, setChallenge] = useState(''), [code, setCode] = useState('');
  const [review, setReview] = useState(null), [conversations, setConversations] = useState([]), [transcript, setTranscript] = useState(null);
  const refresh = async () => setMembers((await accountRequest('/api/account/family')).members || []);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const config = await accountRequest('/auth/options');
        if (!config.enabled || !alive) return;
        const value = await accountRequest('/api/account');
        if (!alive) return;
        setOptions(config); setAccount(value);
        if (config.managed) await refresh();
      } catch (e) { if (alive && e.status !== 404) setError(e.message); }
    })();
    return () => { alive = false; };
  }, []);
  const act = async fn => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await fn(); } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  if (!options) return error ? <p role="alert">{error}</p> : null;
  return <section className="family-settings" aria-label="Account and family settings">
    <h3>Account</h3><p>Signed in as {account?.account?.name}. Connections and personal data belong to your account.</p>
    {options.google && <a className="onboarding-secondary" href="/auth/google/login?link=true">Link Google sign-in</a>}
    {options.github && <a className="onboarding-secondary" href="/auth/login?link=true">Link GitHub sign-in</a>}
    {options.email && <details><summary>Add email sign-in to this account</summary><form onSubmit={e => { e.preventDefault(); act(async () => {
      if (!challenge) setChallenge((await accountRequest('/auth/email/start', 'POST', { email, link: true })).challenge);
      else { await accountRequest('/auth/email/verify', 'POST', { challenge, code }); setChallenge(''); setCode(''); setNotice('Email sign-in linked to this account.'); }
    }); }}>
      <label>Email address<input required disabled={busy || Boolean(challenge)} type="email" value={email} onChange={e => setEmail(e.target.value)} /></label>
      {challenge && <label>Eight-digit code<input required inputMode="numeric" pattern="[0-9]{8}" maxLength={8} value={code} onChange={e => setCode(e.target.value)} /></label>}
      <button className="onboarding-secondary" disabled={busy}>{challenge ? 'Verify and link email' : 'Send sign-in code'}</button>
    </form></details>}
    {account?.can_invite && <details><summary>Invite someone to the preview</summary><form onSubmit={e => { e.preventDefault(); act(async () => {
      await accountRequest('/api/account/invitations', 'POST', { recipient }); setRecipient(''); setNotice('Invitation saved for seven days. Share the Kyrex address with them.');
    }); }}><label>Email address or github:username<input required maxLength={254} value={recipient} onChange={e => setRecipient(e.target.value)} /></label><button className="onboarding-secondary" disabled={busy}>Allow invited sign-up</button></form></details>}
    {options.managed && <>
      <h3>Family</h3><p>Each managed account has its own identity, devices, and conversations. Managed chat never inherits your Gmail, Calendar, repositories, or Bot permissions.</p>
      <details><summary>Create a parent-managed account</summary>
        <form onSubmit={e => { e.preventDefault(); act(async () => {
          await accountRequest('/api/account/family', 'POST', { name, age_band: age, consent }); setName(''); setConsent(false); await refresh();
        }); }}>
          <label>Nickname<input required disabled={busy} maxLength={80} autoComplete="off" value={name} onChange={e => setName(e.target.value)} /></label>
          <label>Age range<select value={age} disabled={busy} onChange={e => setAge(e.target.value)}><option value="under13">Under 13</option><option value="13to17">13–17</option></select></label>
          <p>Chats are stored for you to review and sent to the learning provider you select. You can pause access, revoke devices, or delete chats and the account. The learner sees that you can review their chats. Deletion from Kyrex does not delete copies retained by the model provider.</p>
          <label className="account-checkbox"><input required type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} /> I am this child’s parent or legal guardian and approve this configured private pilot.</label>
          <button className="onboarding-secondary" disabled={busy || !consent}>Create managed account</button>
        </form>
      </details>
      {members.map(member => <Member key={member.id} member={member} profiles={profiles} busy={busy} act={act} onRefresh={refresh} onReview={member => act(async () => {
        setReview(member); setTranscript(null); setConversations((await accountRequest('/api/account/family/' + member.owner + '/conversations')).conversations || []);
      })} />)}
      {review && <section aria-label="Managed conversation review"><h4>{review.name}’s chats</h4>
        <button className="onboarding-secondary" onClick={() => { setReview(null); setTranscript(null); }}>Close review</button>
        {conversations.map(c => <button className="onboarding-secondary" key={c.conversation_id} disabled={busy} onClick={() => act(async () => {
          setTranscript(await accountRequest('/api/account/family/' + review.owner + '/conversations/' + c.conversation_id));
        })}>{c.title}</button>)}
        {!conversations.length && <p>No conversations yet.</p>}
        {transcript?.messages?.map((m, i) => <article className="family-transcript" key={m.id || i}><strong>{m.role === 'user' ? review.name : 'Kyrex'}</strong><p>{m.content}</p></article>)}
      </section>}
    </>}
    {notice && <p role="status">{notice}</p>}{error && <p className="message-error" role="alert">{error}</p>}
  </section>;
}
