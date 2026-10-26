export const isKbotSurface = (location = window.location) =>
  new URLSearchParams(location.search).get('surface') === 'kbot';

export function kbotPhase(voicePhase, generating, lastMessage) {
  if (voicePhase === 'speaking') return 'speaking';
  if (['listening', 'recording', 'followup'].includes(voicePhase)) return 'listening';
  if (['starting', 'loading', 'transcribing', 'working', 'synthesizing', 'interrupting', 'followup_wait'].includes(voicePhase)) return 'thinking';
  if (generating || lastMessage?.durable_active || lastMessage?.delegated_active || ['queued', 'running'].includes(lastMessage?.task?.status)) {
    return lastMessage?.role === 'assistant' && lastMessage.streaming && lastMessage.content ? 'replying' : 'thinking';
  }
  return 'idle';
}

export function readKbotAnchor(value) {
  if (!value || !['left', 'right'].includes(value.side) || !Number.isFinite(value.y)) return null;
  const anchor = { side: value.side, y: Math.max(28, Math.min(window.innerHeight - 28, value.y)) };
  if (['compact', 'medium', 'full'].includes(value.size)) anchor.size = value.size;
  return anchor;
}

export function resizeKbot(size, navigate = path => window.location.assign(path)) {
  if (!['compact', 'medium', 'full'].includes(size)) return false;
  navigate(`/__companion__/size/${size}`);
  return true;
}

export const closeKbot = () => window.location.assign('/__companion__/hide');
