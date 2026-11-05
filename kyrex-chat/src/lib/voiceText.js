// Presentation only: preserve the completed reply's facts and approval wording.
// No second model call, and no speech from provisional model/tool deltas.
const finish = text => /[.!?:;]["'”’)]*$/.test(text) ? text : text + '.';
const inline = text => text
  .replace(/!?\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)/g, '$1')
  .replace(/https?:\/\/[^\s<>]+/g, url => 'link in chat' + (url.match(/[.!?;,)]+$/)?.[0] || ''))
  .replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, (_, a, b) => a || b)
  .replace(/\*(\S(?:[^*\n]*\S)?)\*|(?<!\w)_(\S(?:[^_\n]*\S)?)_(?!\w)/g, (_, a, b) => a || b)
  .replace(/`([^`]+)`/g, '$1')
  .replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/\s+\|\s+/g, ', ').replace(/\s+/g, ' ').trim();

const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '')
  .split(/(?<!\\)\|/).map(cell => inline(cell.replace(/\\\|/g, '|')));

export function spokenText(raw = '') {
  // Bound formatting work before processing model output. Truncated code stays
  // hidden, including an opening fence with no closing fence in this window.
  const source = String(raw).slice(0, 12000);
  const lines = source.replace(/```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)/g,
    '\nCode details are in your voice chat.\n').split(/\r?\n/);
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line || /^\s*(?:[-*_]\s*){3,}$/.test(line)) continue;
    if (line.includes('|') && /^\s*\|?\s*:?-{3,}:?\s*\|/.test(lines[i + 1] || '')) {
      const headers = cells(line); i++;
      while (i + 1 < lines.length && lines[i + 1].includes('|')) {
        const row = cells(lines[++i]);
        out.push(row.map((value, index) => value ? finish((headers[index] ? headers[index] + ': ' : '') + value) : '').filter(Boolean).join(' '));
      }
      continue;
    }
    const structured = /^(?:#{1,6}\s+|[-+*•]\s+|\d+[.)]\s+|>\s*)/.test(line) || /^(?:\*\*[^*]+\*\*|__[^_]+__)$/.test(line);
    const value = inline(line.replace(/^(?:#{1,6}\s+|[-+*•]\s+|\d+[.)]\s+|>\s*)/, '')
      .replace(/^\[([ xX])\]\s*/, (_, mark) => mark === ' ' ? 'Unchecked: ' : 'Checked: '));
    if (value) out.push(structured ? finish(value) : value);
  }
  let value = out.join(' ').replace(/\s+/g, ' ').trim();
  if (value.length > 1600 || source.length < String(raw).length) {
    let prefix = value.slice(0, 1600);
    if (/[\uD800-\uDBFF]$/.test(prefix)) prefix = prefix.slice(0, -1);
    const boundaries = sentenceEnds(prefix);
    const end = boundaries.at(-1) >= 800 ? boundaries.at(-1) : prefix.lastIndexOf(' ');
    value = finish(prefix.slice(0, end > 0 ? end : prefix.length).trim()) + ' The full response is in your voice chat.';
  }
  return value || 'Open your voice chat to review the result.';
}

function sentenceEnds(text) {
  return [...text.matchAll(/[.!?]["'”’)]*(?=\s|$)/g)]
    .filter(match => !/(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs)|\b[A-Z]|\b[ap]\.m)\.$/i.test(text.slice(0, match.index + match[0].length)))
    .map(match => match.index + match[0].length);
}

export function speechChunks(text) {
  const parts = [];
  let rest = text.trim();
  while (rest) {
    // A shorter first piece reduces synthesis time before the first audio.
    // Later pieces retain enough sentence context for natural prosody.
    const maximum = parts.length ? 400 : 180;
    if (rest.length <= maximum) { parts.push(rest); break; }
    const prefix = rest.slice(0, maximum + 1);
    let end = sentenceEnds(prefix).filter(end => end <= maximum).at(-1) || 0;
    if (!end) end = prefix.lastIndexOf(' ');
    if (end <= 0) end = maximum;
    if (/[\uD800-\uDBFF]/.test(rest[end - 1]) && /[\uDC00-\uDFFF]/.test(rest[end])) end--;
    parts.push(rest.slice(0, end).trim());
    rest = rest.slice(end).trimStart();
  }
  return parts;
}
