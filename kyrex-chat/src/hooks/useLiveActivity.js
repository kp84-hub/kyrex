import { useEffect, useState } from 'react';

// useLiveActivity — follow each active task's durable Flux stream (SSE).
//
// This is what makes a conversation's active-work line update LIVE with NO
// polling: the browser subscribes to the SAME durable task event stream the
// executor already writes to (`/api/task/{id}/events`, flux.py), and folds
// each status frame into an overlay the sidebar renders.
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
      if (s && s.taskId && !byTask.has(s.taskId)) byTask.set(s.taskId, s);
    }

    const sources = [];
    for (const [taskId, s] of byTask) {
      let es;
      try {
        es = new EventSource(`/api/task/${encodeURIComponent(taskId)}/events`);
      } catch {
        continue; // unsupported / malformed — the durable list still shows
      }
      sources.push(es);

      const apply = (status) => {
        if (!status) return;
        setOverlay((prev) => ({
          ...prev,
          [s.conversationId]: {
            ...(s.activity || {}),
            task_id: taskId,
            status,
          },
        }));
      };

      es.addEventListener('status', (ev) => {
        try {
          apply(JSON.parse(ev.data).status);
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
        try {
          es.close(); // never let EventSource auto-reconnect a finished task
        } catch {
          /* noop */
        }
      });
      // Any transport/auth error closes the stream; the durable line remains.
      es.addEventListener('error', () => {
        try {
          es.close();
        } catch {
          /* noop */
        }
      });
    }

    return () => {
      for (const es of sources) {
        try {
          es.close();
        } catch {
          /* noop */
        }
      }
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  return overlay;
}
