import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Keep the actual answer intact. This is a display preview, never a generated
// claim about success. Links and verification/blocker lines stay discoverable.
export function resultPreview(text) {
  const paragraphs = text.split(/\n\s*\n/);
  const intro = paragraphs[0].slice(0, 300);
  const lines = text.split('\n');
  const blockers = lines.filter(line => /\b(?:failed|blocked|untested|unverified|pending|incomplete|not (?:tested|run|merged|deployed))\b/i.test(line));
  const links = lines.filter(line => /https:\/\/github\.com\/[^\s)]+\/pull\/\d+/.test(line));
  const checks = lines.filter(line => /\b(?:tests?|checks?|passed)\b/i.test(line));
  const selected = [...new Set([...blockers, ...links, ...checks])]
    .filter(line => !intro.includes(line)).slice(0, 3);
  return [intro + (intro.length < paragraphs[0].length ? '…' : ''),
    ...selected.map(line => line.slice(0, 140))].join('\n\n');
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
