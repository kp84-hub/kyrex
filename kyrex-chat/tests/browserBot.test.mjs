// browserBot.test.mjs — the Browser Bot surface's deterministic decisions.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `browser_bot`
//      flag — never re-derived from the allowlist, the policy, or a local
//      guess, and a non-boolean server value is not trusted;
//   2. the entitlement preview lists the browser preset's effective,
//      host-derived permissions verbatim, sorted: `browser:navigate` and
//      `browser:read` are the ONLY granted operations, and every interaction /
//      write capability stays denied;
//   3. the enable gate matches the server's rules — a non-empty browser
//      allowlist AND an explicit Browser Host binding are both required.
//
// Run: node tests/browserBot.test.mjs
import assert from "node:assert/strict";
import {
  BROWSER_BOT_ALLOWED_OPS,
  BROWSER_BOT_BADGE_LABEL,
  canEnableBrowserBot,
  browserBotBadge,
  browserBotBlockers,
  browserBotPermissionRows,
} from "../src/lib/browserBot.js";

// ── 1. the badge is a pure function of SERVER state ──────────────────
{
  assert.equal(browserBotBadge({ browser_bot: true }), true);
  assert.equal(browserBotBadge({ browser_bot: false }), false);
  assert.equal(browserBotBadge({}), false);
  assert.equal(browserBotBadge(null), false);
  assert.equal(browserBotBadge(undefined), false);
  // A non-boolean server value is NOT trusted (exact `true` only).
  assert.equal(browserBotBadge({ browser_bot: "true" }), false);
  assert.equal(browserBotBadge({ browser_bot: 1 }), false);
  // The UI never infers the badge from the allowlist or a preset name.
  assert.equal(
    browserBotBadge({ browser_allowlist: ["example.com"] }), false,
    "the allowlist alone must not produce a badge");
  assert.equal(BROWSER_BOT_BADGE_LABEL, "Browser Bot");
}

// ── 2. the granted operations are exactly navigate + read ────────────
{
  assert.deepEqual(
    [...BROWSER_BOT_ALLOWED_OPS].sort(),
    ["browser:navigate", "browser:read"],
    "a Browser Bot is granted navigation and page reading only");
  // The write/interaction ops are NOT in the allowed set.
  for (const op of [
    "browser:click", "browser:screenshot", "browser:type",
    "browser:upload", "browser:download", "browser:submit",
    "browser:delete", "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
  ]) {
    assert.equal(BROWSER_BOT_ALLOWED_OPS.includes(op), false,
      `${op} must never be allowed`);
  }
}

// The effective permissions the server returns for the browser preset.
const BROWSER_PERMS = {
  "browser:navigate": 0, "browser:read": 0,
  "browser:click": "deny", "browser:screenshot": "deny",
  "browser:type": "deny", "browser:upload": "deny",
  "browser:download": "deny", "browser:submit": "deny",
  "browser:delete": "deny",
  "fs:read": "deny", "fs:write": "deny", "fs:delete": "deny",
  "repo:read": "deny", "repo:pr": "deny", "repo:push": "deny",
  "cal:list": "deny", "cal:create": "deny", "mail:read": "deny",
  "mail:send": "deny", "bot:delegate": "deny",
};

// ── 3. the entitlement preview is verbatim + sorted ──────────────────
{
  const rows = browserBotPermissionRows({ permissions: BROWSER_PERMS });
  // Sorted by operation name.
  const ops = rows.map(([op]) => op);
  assert.deepEqual(ops, [...ops].sort());
  // Exactly two granted ops — navigate + read — and nothing else.
  const granted = rows.filter(([, tier]) => tier === 0).map(([op]) => op);
  assert.deepEqual(granted.sort(), ["browser:navigate", "browser:read"]);
  // Every write/interaction op is denied.
  for (const [op, tier] of rows) {
    if (!BROWSER_BOT_ALLOWED_OPS.includes(op)) {
      assert.equal(tier, "deny", `${op} must be denied (got ${tier})`);
    }
  }
  // Degenerate inputs degrade safely (never throw).
  assert.deepEqual(browserBotPermissionRows(null), []);
  assert.deepEqual(browserBotPermissionRows({}), []);
  assert.deepEqual(browserBotPermissionRows({ permissions: null }), []);
}

// ── 4. the enable gate requires allowlist AND host binding ───────────
{
  const withHost = { boundHostId: "ovh-ny-01" };
  const botWithAllow = { browser_allowlist: ["example.com"] };
  const botNoAllow = { browser_allowlist: [] };

  // Both missing.
  assert.deepEqual(
    browserBotBlockers(botNoAllow, {}).length, 2,
    "empty allowlist + no host = two blockers");
  // Allowlist only.
  assert.deepEqual(
    browserBotBlockers(botWithAllow, {}),
    ["an explicit Browser Host binding"]);
  // Host binding only.
  assert.deepEqual(
    browserBotBlockers(botNoAllow, withHost),
    ["a non-empty browser domain allowlist"]);
  // Both present.
  assert.deepEqual(browserBotBlockers(botWithAllow, withHost), []);
  assert.equal(canEnableBrowserBot(botWithAllow, withHost), true);
  assert.equal(canEnableBrowserBot(botNoAllow, withHost), false);
  assert.equal(canEnableBrowserBot(botWithAllow, {}), false);
  assert.equal(canEnableBrowserBot(null, {}), false);
  // A malformed allowlist (non-array) fails closed.
  assert.equal(
    canEnableBrowserBot({ browser_allowlist: "example.com" }, withHost), false);
}
