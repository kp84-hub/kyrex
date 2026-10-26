import React, { useEffect, useState } from 'react';
import { reserveConsentWindow, navigateConsentWindow, closeConsentWindow, consentUrl } from '../lib/consentWindow.js';
import { developerRepository, selectDeveloperRepository, reviewDeveloperRepository,
  publishDeveloperRepository, refreshDeveloperRepository, connectGitHub, githubPublisher, updateBotStatus } from '../lib/api.js';

export default function DeveloperRepository({ bot, onChanged }) {
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState(null);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [review, setReview] = useState(null);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [pr, setPr] = useState(null);
  const [signIn, setSignIn] = useState('');
  const paused = bot.status !== 'running';
  async function load() {
    const next = await developerRepository(bot.id);
    setInfo(next); setSelected(next.repository || '');
  }
  async function act(fn) {
    setBusy(true); setError('');
    try { await fn(); } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  function authorize(start) {
    const popup = reserveConsentWindow();
    act(async () => {
      try {
        const result = await start();
        const url = consentUrl(result.authorization_url);
        setSignIn(url);
        setNotice(navigateConsentWindow(popup, url) ? 'Finish GitHub sign-in, then refresh repositories.' : 'Open the sign-in page below, then refresh repositories.');
      } catch (e) { closeConsentWindow(popup); throw e; }
    });
  }
  useEffect(() => {
    if (!open) return;
    let alive = true;
    developerRepository(bot.id).then((next) => {
      if (alive) { setInfo(next); setSelected(next.repository || ''); }
    }).catch((e) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [open, bot.id]);
  useEffect(() => { setReview(null); }, [bot.status]);
  return <div className="developer-repository">
    <button type="button" className="bot-configure-btn" aria-expanded={open} onClick={() => setOpen(!open)}>
      {open ? 'Close repository' : 'Repository'}
    </button>
    {open && <div className="bot-llm-config" aria-label="Developer repository">
      <p>Choose the project Developer Bot edits. Each repository keeps its own workspace and changes.</p>
      {!paused && <p>Pause this Bot and let its tasks finish before switching repositories or publishing.</p>}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <button type="button" className="bot-configure-btn" disabled={busy}
        onClick={() => act(async () => {
          await updateBotStatus(bot.id, paused ? 'running' : 'paused');
          setReview(null); setPr(null); await onChanged?.();
          setNotice(paused ? 'Bot started. Send your request in this chat.' : 'Bot paused. Choose the repository, then start it here.');
        })}>{paused ? 'Start Bot' : 'Pause Bot'}</button>
      {signIn && <a href={signIn} target="_blank" rel="noreferrer">Open GitHub sign-in page</a>}
      <button type="button" className="bot-configure-btn" disabled={busy} onClick={() => authorize(connectGitHub)}>Connect GitHub reader</button>
      <button type="button" className="bot-configure-btn" disabled={busy} onClick={() => act(load)}>Refresh repositories</button>
      {info && <>
        {info.reader_error && <p role="alert">{info.reader_error}</p>}
        {info.publisher_error && <p role="alert">{info.publisher_error}</p>}
        <div className="bot-config-field">
          <label htmlFor={`repository-${bot.id}`}>Working repository</label>
          <select id={`repository-${bot.id}`} value={selected} disabled={busy || !paused}
            onChange={(e) => { setSelected(e.target.value); setReview(null); setPr(null); }}>
            <option value="">Original workspace</option>
            {info.repository && !info.repositories.includes(info.repository) && <option value={info.repository} disabled>{info.repository} (access unavailable)</option>}
            {info.repositories.map((repo) => <option key={repo} value={repo}>{repo}</option>)}
          </select>
        </div>
        <button type="button" className="bot-configure-btn" disabled={busy || !paused || selected === (info.repository || '')}
          onClick={() => act(async () => {
            await selectDeveloperRepository(bot.id, selected || null);
            setReview(null); setPr(null); await load(); await onChanged?.();
            setNotice('Repository selected. Start Bot here to work on this project.');
          })}>{busy ? 'Working…' : 'Use repository'}</button>
        <p>Current: {info.repository || 'original workspace'}{info.base ? ` · base ${info.base}` : ''}</p>
        {info.workspace && <p>Compared with the last fetched base: {info.workspace.behind ?? '?'} commits behind,
          {' '}{info.workspace.ahead ?? '?'} local commits ahead.
          {info.workspace.reason ? ` ${info.workspace.reason}` : ''}</p>}
        <button type="button" className="bot-configure-btn" disabled={busy || !paused || !info.repository}
          onClick={() => act(async () => {
            setReview(null); setPr(null);
            const result = await refreshDeveloperRepository(bot.id);
            await load();
            setNotice(result.workspace.reason || 'Checkout checked against the latest base. Local work was preserved.');
          })}>Refresh checkout</button>
        <p>Publishing needs separate Contents and Pull requests write permission for this repository.</p>
        <button type="button" className="bot-configure-btn" disabled={busy} onClick={() => authorize(() => githubPublisher(info.publisher_connected ? 'manage' : 'connect'))}>{info.publisher_connected ? 'Manage publishing repositories' : 'Enable PR publishing'}</button>
        {info.publisher_connected && <button type="button" className="bot-configure-btn" disabled={busy} onClick={() => act(async () => {
          await githubPublisher('disconnect'); await load(); setReview(null);
        })}>Disconnect publisher</button>}
        <button type="button" className="bot-configure-btn" disabled={busy || !paused || !info.repository} onClick={() => act(async () => {
          setReview(await reviewDeveloperRepository(bot.id)); setPr(null);
        })}>Review changes</button>
        {review && <div aria-label="Pull request review">
          <p>{review.files.length} changed files in {review.repository} → {review.base}</p>
          <pre style={{ maxHeight: '24rem', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {review.diff || 'No changes to publish.'}
          </pre>
          <div className="bot-config-field"><label htmlFor={`pr-title-${bot.id}`}>PR title</label>
            <input id={`pr-title-${bot.id}`} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} /></div>
          <div className="bot-config-field"><label htmlFor={`pr-body-${bot.id}`}>Description and test results</label>
            <textarea id={`pr-body-${bot.id}`} value={body} maxLength={10000} onChange={(e) => setBody(e.target.value)} /></div>
          <button type="button" className="bot-configure-btn" disabled={busy || !paused || !info.can_publish || !title.trim() || !review.files.length || !!pr}
            onClick={() => act(async () => { setPr(await publishDeveloperRepository(bot.id, { snapshot: review.snapshot, title, body })); })}>
            Create pull request
          </button>
          {!info.can_publish && <p>Select this repository in the publisher connection to create a PR.</p>}
        </div>}
        {pr && <p role="status"><a href={pr.url} target="_blank" rel="noreferrer">Open pull request</a></p>}
      </>}
    </div>}
  </div>;
}
