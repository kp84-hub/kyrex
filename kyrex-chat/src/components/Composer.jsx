import React, { useEffect, useRef, useState } from 'react';

export default function Composer({ onSend, onStop, isGenerating, providers = [], activeProvider, activeModel, activeBotId, onChangeProvider, workspaces = [], activeWorkspaceId = null, onAttachWorkspace }) {
  const [text, setText] = useState('');
  const [modelDraft, setModelDraft] = useState(activeModel || '');
  const [isApplyingModel, setIsApplyingModel] = useState(false);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const taRef = useRef(null);
  const workspacePickerRef = useRef(null);
  const workspaceTriggerRef = useRef(null);
  const attachedWorkspace = workspaces.find((workspace) => workspace.id === activeWorkspaceId);

  useEffect(() => {
    setModelDraft(activeModel || '');
  }, [activeModel]);

  const closeWorkspacePicker = (restoreFocus = true) => {
    setWorkspaceOpen(false);
    if (restoreFocus) workspaceTriggerRef.current?.focus();
  };

  useEffect(() => {
    if (!workspaceOpen) return undefined;
    const onPointerDown = (event) => {
      if (workspacePickerRef.current?.contains(event.target)) return;
      const clickedControl = event.target.closest?.('a, button, input, select, textarea, [tabindex]:not([tabindex="-1"])');
      closeWorkspacePicker(!clickedControl);
    };
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeWorkspacePicker();
      }
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [workspaceOpen]);

  // Keep focus in the composer as soon as the page loads and after each turn.
  useEffect(() => {
    taRef.current?.focus();
  }, [isGenerating]);

  const submit = () => {
    const value = text.trim();
    if (!value || isGenerating || isApplyingModel) return;
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

  const applyModel = async () => {
    const model = modelDraft.trim();
    if (!model || !activeProvider || isGenerating || isApplyingModel || activeBotId || !onChangeProvider) return;
    if (model === activeModel) return;
    setIsApplyingModel(true);
    try {
      await onChangeProvider(activeProvider, model);
    } finally {
      setIsApplyingModel(false);
    }
  };

  return (
    <div className="composer">
      <div className="composer-entry-row">
        <div className="workspace-picker" ref={workspacePickerRef}>
          <button
            ref={workspaceTriggerRef}
            type="button"
            className="workspace-attach-btn"
            aria-label="Attach workspace"
            aria-haspopup="menu"
            aria-expanded={workspaceOpen}
            title={activeBotId
              ? 'A Bot-bound conversation uses the Bot’s Rift — workspaces cannot be attached.'
              : attachedWorkspace
                ? 'A repo/workspace is attached — Kyrex can inspect it (read-only). Select “No workspace” to detach.'
                : 'Attach a server-registered workspace (read-only inspection)'}
            disabled={Boolean(activeBotId)}
            onClick={() => setWorkspaceOpen((open) => !open)}
          >+</button>
          {(attachedWorkspace || activeWorkspaceId) && (
            <span className="workspace-active-chip" title="A repo/workspace is attached — Kyrex can inspect it (read-only).">
              {attachedWorkspace?.name || activeWorkspaceId}
            </span>
          )}
          {workspaceOpen && !activeBotId && (
            <div className="workspace-menu" role="menu" aria-label="Select workspace">
              <button
                type="button"
                role="menuitemradio"
                aria-checked={!activeWorkspaceId}
                className="workspace-menu-item"
                onClick={() => { onAttachWorkspace?.(null); closeWorkspacePicker(); }}
              >No workspace</button>
              {workspaces.map((workspace) => (
                <button
                  key={workspace.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={workspace.id === activeWorkspaceId}
                  className="workspace-menu-item"
                  disabled={workspace.available === false}
                  title={workspace.available === false ? 'Workspace is unavailable' : 'Attach this workspace (read-only inspection)'}
                  onClick={() => { onAttachWorkspace?.(workspace.id); closeWorkspacePicker(); }}
                >
                  {workspace.name}{workspace.available === false ? ' (unavailable)' : ''}
                </button>
              ))}
            </div>
          )}
        </div>
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
      </div>
      <div className="composer-actions">
        <div className="composer-model-controls" aria-label="Conversation model">
          <select
            className="composer-model-select"
            value={activeProvider || ''}
            disabled={Boolean(activeBotId) || isGenerating || isApplyingModel || !onChangeProvider}
            onChange={(e) => {
              const profile = providers.find((p) => p.id === e.target.value);
              if (profile?.models?.length) onChangeProvider(profile.id, profile.models.includes(activeModel) ? activeModel : profile.models[0]);
            }}
            aria-label="Provider"
          >
            {providers.map((p) => <option key={p.id} value={p.id}>{p.label || p.id}</option>)}
          </select>
          <input
            className="composer-model-input"
            type="text"
            value={modelDraft}
            maxLength={256}
            disabled={Boolean(activeBotId) || isGenerating || isApplyingModel || !onChangeProvider}
            onChange={(e) => setModelDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                applyModel();
              }
            }}
            placeholder="Paste model ID"
            aria-label="Model ID"
            title="Paste the model ID exactly as your provider lists it, then select Use."
          />
          <button
            type="button"
            className="composer-model-apply"
            onClick={applyModel}
            disabled={Boolean(activeBotId) || isGenerating || isApplyingModel || !onChangeProvider || !modelDraft.trim() || modelDraft.trim() === activeModel}
            aria-label="Use model ID"
          >{isApplyingModel ? 'Saving…' : 'Use'}</button>
        </div>
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
