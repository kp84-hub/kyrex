import { useEffect, useState } from 'react';
import { latestProgressStage } from '../lib/progress.js';
import { isTerminalActivity } from '../lib/activeWork.js';

// useLiveActivity — follow each active task's durable Flux stream (SSE).
//
// This is what makes a conversation's active-work line update LIVE with NO
// polling: the browser subscribes to the SAME durable task event stream the
// executor already writes to (`/api/task/{id}/events`, flux.py), and folds
// each status and named progress frame into an overlay the sidebar renders.
//
// `subs` is the list from activeWork.js `activeSubscriptions`:
//   [{ conversationId, taskId, activity }]
//
// Returns { conversationId: activity } with the latest durable status. A
// terminal status makes buildActivityLines drop the line. A no-op where
// EventSource is unavailable (SSR / jsdom / tests).
export function useLiveActivity(subs) {
  const [overlay, setOverlay] = useState({});

  // Re-subscribe only when the SET of followed tasks changes, not on every
  // render (the parent rebuilds `subs` each cycle).
  const key = Array.isArray(subs)
    ? subs
        .filter((s) => s && s.taskId)
        .map((s) => `${s.conversationId}:${s.taskId}`)
        .sort()
        .join(',')
    : '';

  useEffect(() => {
    if (typeof EventSource === 'undefined') return undefined;

    const byTask = new Map();
    for (const s of subs || []) {
      if (s && s.taskId) {
        if (!byTask.has(s.taskId)) byTask.set(s.taskId, []);
        byTask.get(s.taskId).push(s);
      }
    }

    const sources = [];
    for (const [taskId, followers] of byTask) {
      let es;
      try {
        es = new EventSource(`/api/task/${encodeURIComponent(taskId)}/events`);
      } catch {
        continue; // unsupported / malformed — the durable list still shows
      }
      let closed = false;
      const close = () => {
        closed = true;
        try { es.close(); } catch { /* noop */ }
      };
      sources.push(close);

      const apply = (status) => {
        if (closed || !status) return;
        setOverlay(prev => {
          const next = { ...prev };
          for (const s of followers) {
            next[s.conversationId] = {
              ...(s.activity || {}),
              ...(prev[s.conversationId]?.task_id === taskId ? prev[s.conversationId] : {}),
              task_id: taskId, status,
            };
          }
          return next;
        });
      };

      es.addEventListener('progress', (ev) => {
        if (closed) return;
        try {
          const stage = latestProgressStage([{ kind: 'progress', payload: JSON.parse(ev.data) }]);
          if (!stage) return;
          setOverlay(prev => {
            const next = { ...prev };
            for (const s of followers) {
              const current = prev[s.conversationId]?.task_id === taskId
                ? prev[s.conversationId] : s.activity;
              if (isTerminalActivity(current?.status)) continue;
              next[s.conversationId] = { ...current, task_id: taskId, progress_update: stage };
            }
            return next;
          });
        } catch { /* ignore malformed frame */ }
      });

      es.addEventListener('status', (ev) => {
        try {
          const status = JSON.parse(ev.data).status;
          apply(status);
          if (isTerminalActivity(status)) close();
        } catch {
          /* ignore malformed frame */
        }
      });
      es.addEventListener('submitted', () => apply('queued'));
      es.addEventListener('claimed', () => apply('running'));
      es.addEventListener('end', (ev) => {
        let status = 'done';
        try {
          status = JSON.parse(ev.data).status || status;
        } catch {
          /* default terminal */
        }
        apply(status);
        close(); // never let EventSource auto-reconnect a finished task
      });
      // CONNECTING (0) means the browser is retrying a temporary outage.
      // Keep that same task stream alive; close only a permanent failure.
      // Terminal task events above still close it before any reconnect.
      es.addEventListener('error', () => {
        if (es.readyState !== 0) close();
      });
    }

    return () => {
      for (const close of sources) close();
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  return Object.fromEntries((subs || [])
    .filter(s => s && overlay[s.conversationId]?.task_id === s.taskId)
    .map(s => [s.conversationId, overlay[s.conversationId]]));
}
