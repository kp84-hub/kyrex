// botRosterRefresh.test.mjs — focused coverage for the client Bot-roster
// refresh + deleted-selected-Bot handling in useChat, rendered for real in
// jsdom (React 19) against a mock backend.
//
// Proves:
//   1. the roster is loaded from the authoritative `/api/bots` endpoint on
//      bootstrap (and a Bot-bound conversation restores its selection);
//   2. a refresh PRESERVES the active selection when the Bot still exists;
//   3. a refresh CLEARS the active selection when that Bot was DELETED, and the
//      deleted Bot disappears from the roster (no full page reload needed);
//   4. the focus/visibility boundary refreshes the roster, and is THROTTLED so
//      rapid focus changes never spam the API (no idle polling loop);
//   5. a detecting refresh never throws when the roster request fails (best
//      effort — ordinary chat still works);
//   6. an OLDER, slower /api/bots response that resolves AFTER a newer one
//      cannot restore a Bot the newer response already saw deleted — stale
//      responses are dropped by the request-sequence guard.
//
// Run: node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs tests/botRosterRefresh.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { useChat } from "../src/hooks/useChat.js";

const h = React.createElement;

// ── controllable clock (drives the focus throttle deterministically) ─────
let clock = 1_000_000_000;
const realNow = Date.now;
Date.now = () => clock;

// ── mock backend ────────────────────────────────────────────────────────
// The roster the server returns; mutated between phases to simulate a Bot
// being created or deleted elsewhere.
let roster = [{ id: "chief", name: "Chief of Staff", status: "running" }];
let botsCalls = 0;
let failRoster = false;
// When true, `/api/bots` does NOT settle immediately: it queues a resolver so a
// test can control the ORDER in which in-flight roster responses resolve.
let deferBots = false;
let botsDeferred = [];

function resp(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    async json() { return body; },
  };
}

globalThis.fetch = async (url) => {
  if (url === "/api/bots") {
    botsCalls += 1;
    if (failRoster) return resp({ detail: "boom" }, 500);
    if (deferBots) {
      return new Promise((resolve) => {
        botsDeferred.push((payload) => resolve(resp({ bots: payload })));
      });
    }
    return resp({ bots: roster });
  }
  if (url === "/api/conversations") {
    return resp({ conversations: [
      { conversation_id: "c1", bot_id: "chief", title: "Chief of Staff" },
    ] });
  }
  if (url === "/api/conversations/c1") {
    return resp({ conversation_id: "c1", bot_id: "chief", messages: [] });
  }
  if (url === "/api/chat/workspaces") return resp({ workspaces: [] });
  if (url === "/api/chat/providers") return resp({ providers: [] });
  if (url === "/api/chat/status") return resp({ available: true });
  return resp({});
};

// ── harness: expose the hook's live state via a probe component ──────────
let latest = null;
function Probe() {
  latest = useChat();
  return null;
}

const flush = async () => { await act(async () => {}); };

async function main() {
  globalThis.localStorage = globalThis.window.localStorage;
  window.localStorage.clear();
  // Restore the previously active conversation, as a real browser refresh does.
  window.localStorage.setItem("kyrex-chat.activeConversationId", "c1");

  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);

  // ── 1. bootstrap loads the roster and restores the Bot binding ────────
  await act(async () => { root.render(h(Probe)); });
  await flush();
  // App drives bootstrap on mount (the hook exposes it); do the same here.
  await act(async () => { await latest.bootstrap(); });
  await flush();
  assert.deepEqual(latest.bots.map((b) => b.id), ["chief"],
    "the roster is loaded from /api/bots");
  assert.equal(latest.activeBotId, "chief",
    "the stored Bot binding is restored from the conversation");
  assert.ok(botsCalls >= 1, "bootstrap fetched the roster");
  console.log("ok - bootstrap loads the roster and restores the Bot binding");

  // ── 2. a refresh PRESERVES an existing selection ─────────────────────
  roster = [
    { id: "chief", name: "Chief of Staff", status: "running" },
    { id: "dev", name: "Developer", status: "running" },
  ];
  await act(async () => { await latest.refreshBots(); });
  await flush();
  assert.deepEqual(latest.bots.map((b) => b.id), ["chief", "dev"],
    "the roster reflects the authoritative list");
  assert.equal(latest.activeBotId, "chief",
    "an existing selection is preserved across a roster refresh");
  console.log("ok - an existing selection is preserved");

  // ── 3. a refresh CLEARS a selection whose Bot was DELETED ────────────
  roster = [{ id: "dev", name: "Developer", status: "running" }]; // chief deleted
  await act(async () => { await latest.refreshBots(); });
  await flush();
  assert.deepEqual(latest.bots.map((b) => b.id), ["dev"],
    "the deleted Bot is gone from the roster (no page reload)");
  assert.equal(latest.activeBotId, null,
    "the deleted Bot's selection is cleared (clean fallback)");
  console.log("ok - a deleted Bot vanishes and its selection is cleared");

  // ── 4. focus boundary refreshes, and is THROTTLED ────────────────────
  // Re-bind to a live Bot so we can observe the delete-through-focus path.
  await act(async () => { latest.loadConversation("c1"); });
  await flush();
  roster = [
    { id: "dev", name: "Developer", status: "running" },
    { id: "scout", name: "Scout", status: "running" },
  ];
  const before = botsCalls;
  clock += 20_000; // past the throttle window
  await act(async () => { window.dispatchEvent(new window.Event("focus")); });
  await flush();
  assert.ok(botsCalls > before, "focus refreshes the roster");
  assert.ok(latest.bots.some((b) => b.id === "scout"),
    "a Bot created elsewhere appears after a focus refresh");
  console.log("ok - focus refreshes the roster past the throttle window");

  // A SECOND focus immediately after is THROTTLED (no extra fetch).
  roster = [{ id: "dev", name: "Developer", status: "running" }];
  const afterFirst = botsCalls;
  await act(async () => { window.dispatchEvent(new window.Event("focus")); });
  await flush();
  assert.equal(botsCalls, afterFirst,
    "a rapid second focus is throttled (no excessive polling)");
  console.log("ok - rapid focus changes are throttled");

  // After the window elapses, focus refreshes again.
  clock += 20_000;
  await act(async () => { window.dispatchEvent(new window.Event("focus")); });
  await flush();
  assert.ok(botsCalls > afterFirst, "focus refreshes again once the window elapses");
  assert.ok(!latest.bots.some((b) => b.id === "scout"),
    "the deleted Scout is gone after the throttled refresh");
  console.log("ok - focus refreshes again after the throttle window elapses");

  // ── 5. a failing roster request is best-effort (never throws) ────────
  failRoster = true;
  const list = await (async () => {
    let out;
    await act(async () => { out = await latest.refreshBots(); });
    return out;
  })();
  await flush();
  assert.deepEqual(list, [], "a failed roster fetch resolves to an empty list");
  assert.deepEqual(latest.bots, [], "a failed roster fetch clears the roster");
  console.log("ok - a failing roster request degrades gracefully");

  // ── 6. an OLDER slower response cannot restore a deleted Bot ─────────
  // Re-bind so a selection exists to be cleared and then (wrongly) restored.
  failRoster = false;
  await act(async () => { await latest.loadConversation("c1"); });
  await flush();
  assert.equal(latest.activeBotId, "chief", "bound to chief before the race");

  // Start two overlapping refreshes and settle them OUT OF ORDER.
  deferBots = true;
  botsDeferred = [];
  let older;
  let newer;
  await act(async () => {
    older = latest.refreshBots(); // started first
    newer = latest.refreshBots(); // started second (newest)
  });
  assert.equal(botsDeferred.length, 2, "two in-flight roster requests");

  // The NEWER request resolves FIRST and reports chief deleted.
  await act(async () => {
    botsDeferred[1]([{ id: "dev", name: "Developer", status: "running" }]);
  });
  await newer;
  await flush();
  assert.deepEqual(latest.bots.map((b) => b.id), ["dev"],
    "the newest response is applied");
  assert.equal(latest.activeBotId, null,
    "the deleted selection is cleared by the newest response");

  // The OLDER request resolves LAST and still lists the deleted Bot.
  let olderResult;
  await act(async () => {
    botsDeferred[0]([
      { id: "chief", name: "Chief of Staff", status: "running" },
      { id: "dev", name: "Developer", status: "running" },
    ]);
  });
  olderResult = await older;
  await flush();
  assert.equal(olderResult, null, "the stale response is dropped (no state write)");
  assert.deepEqual(latest.bots.map((b) => b.id), ["dev"],
    "the older slower response CANNOT restore the deleted Bot");
  assert.equal(latest.activeBotId, null,
    "the cleared selection is not resurrected by the stale response");
  deferBots = false;
  console.log("ok - an older slower response cannot restore a deleted Bot");

  await act(async () => { root.unmount(); });
  container.remove();
  Date.now = realNow;
  console.log("ok - bot roster refresh + deleted-selected-Bot handling");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
