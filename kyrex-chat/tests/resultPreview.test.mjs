import assert from 'node:assert/strict';
import test from 'node:test';
import { resultPreview } from '../src/lib/resultPreview.js';

test('TUI diagnosis keeps uncertainty and does not extract clipped startup paths', () => {
  const finding = 'The TUI may be waiting for the engine to start. I inspected the source, but have not verified the failure on your PC.';
  const context = 'The model name is supplied after engine initialization, so a startup failure could leave both fields unchanged.';
  const detail = 'PlaneExecute raised (bad provider/base_url, missing Python deps) → FATAL: Engine initialization failed on stderr, sys.exit(1). ' + 'Additional internal startup details. '.repeat(30);
  const result = resultPreview(`${finding}\n\n${context}\n\n${detail}\n\nNext check: capture the startup error without sharing credentials.`);
  assert.ok(result.startsWith(finding));
  assert.ok(result.includes(context));
  assert.ok(!result.includes('PlaneExecute'));
  assert.ok(!result.includes('sys.exit'));
  assert.ok(result.length <= 800);
});

test('conditional failures in commands are not promoted to observed blockers', () => {
  const text = 'The cause is still unverified.\n\n```python\nif tests_failed:\n    print("Checks: tests failed")\n```\n\nIf the engine failed, inspect its startup output.\n\n' + 'Other details. '.repeat(100);
  const result = resultPreview(text);
  assert.ok(!result.includes('Checks: tests failed'));
  assert.ok(!result.includes('tests_failed'));
  assert.ok(result.includes('If the engine failed'));
});

test('explicit checks, limits and PR links remain available', () => {
  const url = 'https://github.com/kp84-hub/kyrex/pull/420';
  const text = 'Fixed the parser.\n\n' + 'Detailed implementation evidence. '.repeat(100) + `\nChecks: 7 passed.\nNot deployed.\n[${'Long review label '.repeat(30)}](${url})`;
  const result = resultPreview(text);
  assert.match(result, /Checks: 7 passed/);
  assert.match(result, /Not deployed\./);
  assert.ok(result.includes(`[Pull request](${url})`));
  assert.ok(result.length <= 800);
});

test('PR links in fenced logs are not presented as published results', () => {
  const result = resultPreview('No PR was created.\n\n~~~text\nhttps://github.com/example/project/pull/123\nStatus: complete\n~~~');
  assert.equal(result, 'No PR was created.');
});

test('summary lines are kept whole, including negation', () => {
  const outcome = 'Verification: ' + 'Details '.repeat(130) + 'tests were not run.';
  const result = resultPreview('I inspected the source only.\n\n' + 'Other evidence. '.repeat(100) + '\n' + outcome);
  assert.ok(!result.includes('Verification:'));
  assert.equal(result, 'I inspected the source only.');
});

test('long leads retain later qualifications and technical syntax in the full result', () => {
  const text = 'The cause is unverified. ' + 'a_very_long_command_'.repeat(100);
  const original = text;
  assert.equal(resultPreview(text), 'Open the full result to read the findings.');
  assert.equal(text, original);
  assert.equal(resultPreview('Tests passed. ' + 'Details '.repeat(150) + 'But the app still fails.'), 'Open the full result to read the findings.');
  assert.equal(resultPreview('Recommendation: ' + 'Render meaningful progress updates '.repeat(30)), 'Open the full result to read the findings.');
});

test('recommendation previews retain existing focused investigation behavior', () => {
  const trace = '[Delegated to dev] Traced the full chain: ' + 'executor → parser → durable event. '.repeat(40);
  const recommendation = 'One practical improvement: show the latest progress stage in the sidebar.';
  const result = resultPreview(`${trace}\n\n${recommendation}\n\nAccess limitation: No live deployment access available.`);
  assert.ok(result.startsWith(recommendation));
  assert.ok(result.includes('Access limitation: No live deployment access available.'));
  assert.ok(!result.includes('Traced the full'));
});
