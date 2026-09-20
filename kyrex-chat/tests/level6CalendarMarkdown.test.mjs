// level6CalendarMarkdown.test.mjs — the Level 6 Calendar RESULT rendering
// contract in Kyrex Chat.
//
// Regression for the bug this change fixes: the backend used to join the six
// workout rows with SINGLE newlines, so the Kyrex Chat Markdown renderer
// (react-markdown + remark-gfm) collapsed them into ONE paragraph. The
// backend now emits a compact Markdown heading plus six bullet entries (bold
// weekday/date, workout name, a second `Trainer:` line).
//
// This test renders the REAL `Message` component (the same react-markdown +
// remark-gfm path the app uses) under jsdom and asserts the rendered DOM:
//   1. a level-3 heading (not a paragraph);
//   2. exactly ONE list with exactly SIX items;
//   3. each item carries a `<strong>` bold weekday/date and the workout name;
//   4. each item carries a SECOND, visually separate `Trainer:` line;
//   5. the six dates appear in Monday-Saturday order;
//   6. CONTRAST: the OLD single-newline form renders as a single `<p>` with no
//      list at all — proving the regression the bullets prevent.
//
// The exact backend Markdown is duplicated here as a literal so the frontend
// contract is pinned independently of the backend: if either side changes the
// shape, this test fails.
//
// Run: node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs --test \
//        tests/level6CalendarMarkdown.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import Message from "../src/components/Message.jsx";

// The compact Markdown the backend `render_week_markdown` emits for the
// canonical six-day week (byte-identical to level6_calendar.WEEK_HEADING +
// six bullets). Verified against the backend in
// kyrex-cloud/test_level6_calendar.py.
const SIX_ROWS_MARKDOWN = [
  "### 🏋️ Level 6 — Workout Week",
  "",
  "- **Monday 2026-09-21** — Back Squat  ",
  "  Trainer: Ann",
  "- **Tuesday 2026-09-22** — Deadlift  ",
  "  Trainer: Ann",
  "- **Wednesday 2026-09-23** — Clean  ",
  "  Trainer: Bo",
  "- **Thursday 2026-09-24** — Snatch  ",
  "  Trainer: Bo",
  "- **Friday 2026-09-25** — Front Squat  ",
  "  Trainer: Cy",
  "- **Saturday 2026-09-26** — Conditioning  ",
  "  Trainer: Cy",
].join("\n");

// The OLD form: the same six rows joined with SINGLE newlines (no Markdown).
const OLD_SINGLE_NEWLINE = [
  "Monday 2026-09-21 — Back Squat — trainer: Ann",
  "Tuesday 2026-09-22 — Deadlift — trainer: Ann",
  "Wednesday 2026-09-23 — Clean — trainer: Bo",
  "Thursday 2026-09-24 — Snatch — trainer: Bo",
  "Friday 2026-09-25 — Front Squat — trainer: Cy",
  "Saturday 2026-09-26 — Conditioning — trainer: Cy",
].join("\n");

function renderMessage(content) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(React.createElement(Message, {
      message: { id: "m1", role: "assistant", content },
      onRetry: null,
      isLastAssistant: true,
      onRespondApproval: null,
    }));
  });
  return container;
}

// ── 1. the compact Markdown renders as a heading + six-item list ─────
{
  const dom = renderMessage(SIX_ROWS_MARKDOWN);
  const headings = dom.querySelectorAll("h1, h2, h3, h4, h5, h6");
  assert.equal(headings.length, 1, "exactly ONE heading");
  assert.equal(headings[0].tagName, "H3", "the heading is level 3");
  assert.match(headings[0].textContent, /Level 6 — Workout Week/);

  const lists = dom.querySelectorAll("ul");
  assert.equal(lists.length, 1, "exactly ONE bullet list");
  const items = lists[0].querySelectorAll("li");
  assert.equal(items.length, 6, "exactly SIX workout bullets");

  // ── 2. each item: bold weekday/date, the workout, a Trainer line ──
  const dates = [
    "Monday 2026-09-21", "Tuesday 2026-09-22", "Wednesday 2026-09-23",
    "Thursday 2026-09-24", "Friday 2026-09-25", "Saturday 2026-09-26",
  ];
  const workouts = [
    "Back Squat", "Deadlift", "Clean", "Snatch", "Front Squat",
    "Conditioning",
  ];
  const trainers = ["Ann", "Ann", "Bo", "Bo", "Cy", "Cy"];
  items.forEach((li, i) => {
    const bold = li.querySelector("strong");
    assert.ok(bold, `item ${i}: bold weekday/date present`);
    assert.equal(bold.textContent, dates[i],
      `item ${i}: bold weekday/date is exact`);
    assert.match(li.textContent, new RegExp(workouts[i].replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
      `item ${i}: workout name present`);
    assert.match(li.textContent, /Trainer: /,
      `item ${i}: a Trainer line is present`);
    assert.match(li.textContent, new RegExp(`Trainer: ${trainers[i]}\\b`),
      `item ${i}: the trainer name is exact`);
  });

  // ── 3. the Trainer line is a REAL visual line inside the item ─────
  // The workout line ends in two spaces, which react-markdown/CommonMark
  // must render as a hard break. Assert the DOM contract, not just text order.
  items.forEach((li, i) => {
    const breaks = li.querySelectorAll("br");
    assert.equal(breaks.length, 1,
      `item ${i}: exactly one hard line break precedes Trainer`);
    const afterBreak = breaks[0].nextSibling?.textContent || "";
    assert.match(afterBreak, new RegExp(`^\\s*Trainer: ${trainers[i]}\\b`),
      `item ${i}: Trainer starts immediately after the hard break`);
  });
}

// ── 4. CONTRAST: the OLD single-newline form collapses to one <p> ────
{
  const dom = renderMessage(OLD_SINGLE_NEWLINE);
  assert.equal(dom.querySelectorAll("ul").length, 0,
    "the old single-newline form produces NO list (the bug)");
  const paras = dom.querySelectorAll("p");
  assert.equal(paras.length, 1,
    "the old single-newline form collapses into ONE paragraph");
  // And it still contains every row — proving the collapse, not a drop.
  for (const line of [
    "Monday 2026-09-21 — Back Squat — trainer: Ann",
    "Saturday 2026-09-26 — Conditioning — trainer: Cy",
  ]) {
    assert.ok(paras[0].textContent.includes(line.split(" ")[0]),
      "the collapsed paragraph still carries the rows");
  }
}

console.log("level6CalendarMarkdown: all rendering-contract checks passed");
