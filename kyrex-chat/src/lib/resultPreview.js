// Display only: preserve the stored answer and never infer a task outcome.
// Whole paragraphs retain the conditions around a diagnosis. Outcome lines
// must be explicit summaries, not commands/logs matching "failed" or "tests".
const RECOMMENDATION = /^\s*[>#*\s-]*(?:one practical improvement|recommendation|suggested improvement|next step)\s*:/i;
const OUTCOME = /^\s*(?:[-*>]\s*)?(?:\*\*)?(?:(?:checks?|tests?|verification|status|blocker|access limitation|limitations?|remaining|not (?:tested|run|merged|deployed))\b)(?:\*\*)?\s*(?::|\.|$)/i;
const BUDGET = 800;

function outsideFences(text) {
  let fence = null;
  return text.split('\n').map(line => {
    const marker = line.match(/^\s*(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null;
      return '';
    }
    return fence ? '' : line;
  }).join('\n');
}

export function resultPreview(text) {
  const visible = outsideFences(String(text || ''));
  const paragraphs = visible.split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
  const lead = paragraphs.find(p => RECOMMENDATION.test(p)) || paragraphs[0] || '';
  const selected = [];
  let length = 0;
  const add = block => {
    if (!block || selected.some(p => p.includes(block)) || length + block.length + 2 > BUDGET) return false;
    selected.push(block); length += block.length + 2; return true;
  };
  // Keep the entire lead. Even a sentence-boundary cutoff can lose a later
  // qualification ("tests passed, but..."). An oversized lead stays in the
  // full result rather than becoming a partial diagnosis.
  if (!add(lead)) {
    add('Open the full result to read the findings.');
  }
  // Give explicit verification and limits priority, but keep whole lines.
  const outcomes = visible.split('\n').filter(line => OUTCOME.test(line));
  for (const line of outcomes) add(line.trim());
  // Preserve discovered PR links as complete Markdown links, never excerpts
  // of their potentially long labels. Match only outside fenced command output.
  const links = [...new Set(visible.match(/https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+\b/g) || [])];
  for (const url of links) add(`[Pull request](${url})`);
  // The next contiguous paragraph can carry the evidence/uncertainty boundary.
  // Do not skip ahead to arbitrary keyword matches or rearrange the prose.
  const next = paragraphs[paragraphs.indexOf(lead) + 1];
  if (next && !RECOMMENDATION.test(next)) add(next);
  return selected.join('\n\n') || 'Open the full result to read the findings.';
}
