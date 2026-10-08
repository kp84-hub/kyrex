import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { truncateLine } from '../lib/activeWork.js';

// Keep the actual answer intact. This is a display preview, never a generated
// claim about success. Links and verification/blocker lines stay discoverable.
export function resultPreview(text) {
  const paragraphs = text.split(/\n\s*\n/);
  const lead = paragraphs.find(paragraph => /^\s*(?:[>#*\s-]*)(?:one practical improvement|recommendation|suggested improvement|next step)\s*:/i.test(paragraph)) || paragraphs[0];
  const intro = truncateLine(lead, 300);
  const lines = text.split('\n');
  const blockers = lines.filter(line => /\b(?:failed|blocked|untested|unverified|pending|incomplete|limitations?|not (?:tested|run|merged|deployed))\b/i.test(line));
  const links = [...new Set(text.match(/https:\/\/github\.com\/[^\s)]+\/pull\/\d+/g) || [])];
  const checks = lines.filter(line => /\b(?:tests?|checks?|passed)\b/i.test(line));
  const selected = [...new Set([...blockers, ...links, ...checks])]
    .filter(line => !lead.includes(line)).slice(0, 3);
  return [intro, ...selected.map(line => truncateLine(line, 140))].join('\n\n');
}

export default function WorkResult({ text, components }) {
  const markdown = value => <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{value}</ReactMarkdown>;
  if (text.length <= 1000) return markdown(text);
  return <>
    <div className="work-result-preview">{markdown(resultPreview(text))}</div>
    <details className="work-result-details">
      <summary>Full result</summary>
      {markdown(text)}
    </details>
  </>;
}
