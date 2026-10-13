import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { resultPreview } from '../lib/resultPreview.js';
export { resultPreview } from '../lib/resultPreview.js';

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
