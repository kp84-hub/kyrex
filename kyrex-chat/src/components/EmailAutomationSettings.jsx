import React, { useEffect, useMemo, useState } from 'react';
import {
  createEmailAutomationRule,
  deleteEmailAutomationRule,
  listBots,
  listConversations,
  listEmailAutomationRules,
  setEmailAutomationRule,
} from '../lib/api.js';

const DEFAULT_SENDER = '';

function conversationLabel(conversation) {
  const title = conversation.title || 'New chat';
  const timestamp = conversation.updated_at || conversation.created_at;
  const date = timestamp && Number.isFinite(Date.parse(timestamp))
    ? new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' })
      .format(new Date(timestamp))
    : '';
  const count = Number(conversation.message_count || 0);
  return [title, date, `${count} messages`].filter(Boolean).join(' · ');
}

export default function EmailAutomationSettings() {
  const [bots, setBots] = useState([]);
  const [conversations, setConversations] = useState([]);
  const [rules, setRules] = useState([]);
  const [sender, setSender] = useState(DEFAULT_SENDER);
  const [botId, setBotId] = useState('');
  const [conversationId, setConversationId] = useState('');
  const [serviceReady, setServiceReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const eligibleBots = useMemo(() => bots.filter(
    (bot) => bot.manageable && bot.status === 'running'), [bots]);
  const eligibleConversations = useMemo(() => conversations.filter(
    (conversation) => conversation.bot_id === botId), [conversations, botId]);

  const refresh = async () => {
    setError('');
    try {
      const [botRows, conversationRows, managed] = await Promise.all([
        listBots(), listConversations(), listEmailAutomationRules(),
      ]);
      const available = botRows.filter((bot) => bot.manageable && bot.status === 'running');
      const saved = managed.rules || [];
      setBots(botRows);
      setConversations(conversationRows);
      setRules(saved);
      setServiceReady(Boolean(managed.service_ready));
      setBotId((current) => current && available.some((bot) => bot.id === current)
        ? current
        : (available.find((bot) => bot.id === 'email-bot')
          || available.find((bot) => /email/i.test(bot.name || ''))
          || available[0])?.id || '');
    } catch (err) {
      setError(err.message || 'Could not load email automation settings.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  useEffect(() => {
    const latest = eligibleConversations[0];
    if (latest && !eligibleConversations.some((row) => row.conversation_id === conversationId)) {
      setConversationId(latest.conversation_id);
    } else if (!latest) {
      setConversationId('');
    }
  }, [eligibleConversations, conversationId]);

  const save = async (event) => {
    event.preventDefault();
    setError('');
    setSaving(true);
    try {
      await createEmailAutomationRule({ sender: sender.trim(), bot_id: botId,
        conversation_id: conversationId });
      await refresh();
    } catch (err) {
      setError(err.message || 'Could not save this email rule.');
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (rule) => {
    setError('');
    try {
      await setEmailAutomationRule(rule.rule_id, !rule.enabled);
      await refresh();
    } catch (err) { setError(err.message || 'Could not update this email rule.'); }
  };

  const remove = async (rule) => {
    setError('');
    try {
      await deleteEmailAutomationRule(rule.rule_id);
      await refresh();
    } catch (err) { setError(err.message || 'Could not remove this email rule.'); }
  };

  return (
    <section className="email-automation-settings" aria-label="Email automations">
      <div className="email-automation-heading">
        <div>
          <h3>Email automations</h3>
          <p>Watch one sender and deliver new messages to a bot conversation.</p>
        </div>
        <span className={`email-automation-status ${serviceReady ? 'ready' : ''}`}>
          {serviceReady ? 'Service configured' : 'Service not configured'}
        </span>
      </div>
      {!serviceReady && <p className="email-automation-note">
        You can save a rule now. Monitoring starts after the Railway gateway and VPS watcher are configured.
      </p>}
      <form className="email-automation-form" onSubmit={save}>
        <label>Sender email
          <input type="email" required maxLength={254} value={sender}
            onChange={(event) => setSender(event.target.value)}
            placeholder="Sender email address" />
        </label>
        <label>Bot
          <select required value={botId} onChange={(event) => {
            setBotId(event.target.value);
            setConversationId('');
          }}>
            {eligibleBots.map((bot) => <option key={bot.id} value={bot.id}>{bot.name}</option>)}
          </select>
        </label>
        <label>Conversation
          <select required value={conversationId} onChange={(event) => setConversationId(event.target.value)}>
            {eligibleConversations.length === 0 && <option value="">No conversation for this bot</option>}
            {eligibleConversations.map((conversation) => (
              <option key={conversation.conversation_id} value={conversation.conversation_id}>
                {conversationLabel(conversation)}
              </option>
            ))}
          </select>
        </label>
        <button className="send-btn" type="submit"
          disabled={loading || saving || !botId || !conversationId}>
          {saving ? 'Saving…' : 'Add email rule'}
        </button>
      </form>
      {error && <div className="message-error" role="alert">{error}</div>}
      {rules.length > 0 && <div className="email-automation-rules">
        {rules.map((rule) => <article className="email-automation-rule" key={rule.rule_id}>
          <div>
            <strong>{rule.sender}</strong>
            <span>{rule.bot_name} · {rule.conversation_title}</span>
            <span className="email-automation-status-text">{rule.enabled ? 'Rule enabled' : 'Paused'}</span>
          </div>
          <div className="email-automation-actions">
            <button type="button" className="bot-configure-btn" onClick={() => toggle(rule)}>
              {rule.enabled ? 'Pause' : 'Resume'}
            </button>
            <button type="button" className="conversation-delete visible" onClick={() => remove(rule)}>Remove</button>
          </div>
        </article>)}
      </div>}
    </section>
  );
}
