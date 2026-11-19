import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { Sandbox } from 'railway';

const repo = 'https://github.com/kp84-hub/kyrex.git';
const workspace = '/tmp/kyrex-rift';
const patchFile = '/tmp/kyrex-rift.patch';
const tests = 'node --test kyrex-chat/tests/sanitize.test.mjs kyrex-chat/tests/smoothStreaming.test.mjs';
const hash = /^[0-9a-f]{40}$/;

export async function verifyRift({ create, token, environmentId, base, tree, patch, signal, progress }) {
  if (!hash.test(base) || !hash.test(tree)) throw new Error('Invalid snapshot');
  let sandbox;
  let result = { outcome: 'unavailable' };
  let stage = 'starting';
  try {
    signal.throwIfAborted();
    sandbox = await create({ token, authType: 'project-token', environmentId,
      networkIsolation: 'ISOLATED', idleTimeoutMinutes: 2,
      resources: { cpu: 1, memoryGB: 1 } });
    signal.throwIfAborted();
    stage = 'copying';
    progress(stage);
    const checkout = await sandbox.exec(
      `git init -q ${workspace} && git -C ${workspace} -c http.followRedirects=false fetch -q --no-tags --depth=1 ${repo} ${base} && git -C ${workspace} -c core.hooksPath=/dev/null checkout -q --detach FETCH_HEAD`,
      { signal, timeoutSec: 40, captureOutput: false });
    if (checkout.exitCode !== 0 || checkout.timedOut) return { outcome: 'unavailable', stage };

    if (patch.length) await sandbox.files.write(patchFile, patch, { mode: 0o600 });
    const applied = await sandbox.exec(
      `test "$(git rev-parse HEAD)" = ${base} && ` +
      (patch.length ? `git apply --index --binary ${patchFile} && ` : '') +
      `test "$(git write-tree)" = ${tree}`,
      { cwd: workspace, signal, timeoutSec: 20, captureOutput: false });
    if (applied.exitCode !== 0 || applied.timedOut) return { outcome: 'unavailable', stage };

    stage = 'testing';
    progress(stage);
    const check = await sandbox.exec(tests, { cwd: workspace, signal, timeoutSec: 60, captureOutput: false });
    result = check.timedOut ? { outcome: 'timeout' }
      : Number.isInteger(check.exitCode) && check.exitCode >= 0 && check.exitCode <= 255
        ? { outcome: check.exitCode === 0 ? 'success' : 'failure', exitCode: check.exitCode }
        : { outcome: 'unavailable', stage };
  } catch {
    result = { outcome: signal.aborted ? 'cancelled' : 'unavailable', stage };
  } finally {
    if (sandbox) {
      progress('cleaning');
      try {
        await sandbox.destroy();
      } catch {
        return { outcome: 'unavailable', stage: 'cleaning' };
      }
    }
  }
  return result;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const controller = new AbortController();
  process.once('SIGTERM', () => controller.abort());
  const [environmentId, base, tree, patchPath] = process.argv.slice(2);
  const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
  try {
    const patch = patchPath ? await readFile(patchPath) : Buffer.alloc(0);
    emit({ result: await verifyRift({ create: options => Sandbox.create(options),
      token: process.env.KYREX_RIFT_SANDBOX_TOKEN, environmentId, base, tree, patch,
      signal: controller.signal, progress: stage => emit({ stage }) }) });
  } catch {
    emit({ result: { outcome: 'unavailable', stage: 'starting' } });
  }
}
