import React, { useEffect, useRef, useState } from 'react';

export default function Composer({ onSend, onStop, isGenerating, providers = [], activeProvider, activeModel, activeBotId, onChangeProvider }) {
  const [text, setText] = useState('');
  const taRef = useRef(null);

  // Keep focus in the composer as soon as the page loads and after each turn.
  useEffect(() => {
    taRef.current?.focus();
  }, [isGenerating]);

  const submit = () => {
    const value = text.trim();
    if (!value || isGenerating) return;
    onSend(value);
    setText('');
    if (taRef.current) taRef.current.style.height = 'auto';
  };

  const handleKeyDown = (e) => {
    // Enter sends; Shift+Enter inserts a newline. IME composition (CJK
    // input) must never trigger a send mid-composition.
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const autoResize = () => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 240) + 'px';
  };

  return (
    <div className="composer">
      <textarea
        ref={taRef}
        className="composer-input"
        placeholder="Message Kyrex…"
        aria-label="Message Kyrex"
        value={text}
        rows={1}
        onChange={(e) => {
          setText(e.target.value);
          autoResize();
        }}
        onKeyDown={handleKeyDown}
        aria-busy={isGenerating}
      />
      <div className="composer-actions">
        <div className="composer-model-controls" aria-label="Conversation model">
          <select
            className="composer-model-select"
            value={activeProvider || ''}
            disabled={Boolean(activeBotId) || isGenerating || !onChangeProvider}
            onChange={(e) => {
              const profile = providers.find((p) => p.id === e.target.value);
              if (profile?.models?.length) onChangeProvider(profile.id, profile.models.includes(activeModel) ? activeModel : profile.models[0]);
            }}
            aria-label="Provider"
          >
            {providers.map((p) => <option key={p.id} value={p.id}>{p.label || p.id}</option>)}
          </select>
          <select
            className="composer-model-select"
            value={activeModel || ''}
            disabled={Boolean(activeBotId) || isGenerating || !onChangeProvider}
            onChange={(e) => onChangeProvider(activeProvider, e.target.value)}
            aria-label="Model"
          >
            {(providers.find((p) => p.id === activeProvider)?.models || []).map((model) => <option key={model} value={model}>{model}</option>)}
          </select>
        </div>
        <span className="composer-hint">
          <span className="hint-full">Enter to send · Shift+Enter newline</span>
          <span className="hint-short">Enter to send</span>
        </span>
        {isGenerating ? (
          <button
            type="button"
            className="send-btn stop"
            onClick={onStop}
            aria-label="Stop generating"
          >
            <span className="stop-icon" aria-hidden="true">■</span> Stop
          </button>
        ) : (
          <button
            type="button"
            className="send-btn"
            onClick={submit}
            disabled={!text.trim()}
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}
