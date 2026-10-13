// Run the actual compiled model and worker configuration, without a microphone.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../kyrex-chat/public');
const assets = path.join(root, 'voice-wake');
const fixtures = process.argv[2];
assert.ok(fixtures, 'Pass the generated fixture directory');
const events = [];
let resolve;
const ready = new Promise(r => { resolve = r; });
const context = { console, require, process, performance, module: { exports: {} }, exports: {},
  __dirname: assets, __filename: path.join(assets, 'sherpa-onnx-wasm-kws-main.js'),
  Buffer, TextEncoder, TextDecoder, URL, WebAssembly, Uint8Array, Float32Array,
  Int32Array, ArrayBuffer, setTimeout, clearTimeout, setInterval, clearInterval };
context.self = { postMessage: event => {
  events.push(event);
  if (event.type === 'ready' || event.type === 'error') resolve(event);
} };
vm.createContext(context);
context.importScripts = (...files) => files.forEach(file =>
  vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), context, { filename: file }));
// Only the asset locator changes for Node. The model and worker settings are real.
vm.runInContext(fs.readFileSync(path.join(root, 'voice-wake-worker.js'), 'utf8')
  .replace("'/voice-wake/' + file", JSON.stringify(assets + '/') + ' + file'), context);
const timeout = setTimeout(() => { console.error('Compiled wake check timed out'); process.exit(1); }, 45000);
ready.then(result => {
  assert.equal(result.type, 'ready', JSON.stringify(result));
  const cases = JSON.parse(fs.readFileSync(path.join(fixtures, 'cases.json'), 'utf8'));
  for (const { index, expected } of cases) {
    vm.runInContext('stream.free(); stream = kws.createStream(); enabled = true;', context);
    events.length = 0;
    const raw = fs.readFileSync(path.join(fixtures, index + '.f32'));
    const samples = new Float32Array(raw.buffer, raw.byteOffset, raw.byteLength / 4);
    for (let offset = 0; offset < samples.length; offset += 1600)
      context.self.onmessage({ data: { type: 'samples', samples: samples.slice(offset, offset + 1600) } });
    assert.ok(!events.some(e => e.type === 'error'), JSON.stringify(events.filter(e => e.type === 'error')));
    const actual = events.some(e => e.type === 'wake');
    if (expected !== null) assert.equal(actual, expected, 'Synthetic wake case ' + (index + 1));
    else console.log('Known near-name case ' + (index + 1) + ': wake=' + actual + ', not an accuracy pass');
    if (expected !== null) console.log('Compiled WASM synthetic case ' + (index + 1) + ': passed');
  }
  clearTimeout(timeout);
  console.log('Bare Kyrex at three speeds, legacy greeting, and negative phrases passed; actual-device accuracy remains unmeasured.');
  process.exit(0);
}).catch(error => { console.error(error); process.exit(1); });
