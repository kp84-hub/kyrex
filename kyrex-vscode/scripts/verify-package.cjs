const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { promisify } = require('node:util');
const yauzl = require('yauzl');
const { runtimeFiles, source } = require('./sync-engine.cjs');
(async () => {
  const manifest = require('../package.json');
  const filename = process.argv[2] || `${manifest.name}-${manifest.version}.vsix`;
  const zip = await promisify(yauzl.open)(path.resolve(filename), { lazyEntries: true });
  const entries = new Map();
  await new Promise((resolve, reject) => {
    zip.on('error', reject);
    zip.on('entry', entry => {
      zip.openReadStream(entry, (err, stream) => {
        if (err) return reject(err);
        const chunks = [];
        stream.on('error', reject);
        stream.on('data', chunk => chunks.push(chunk));
        stream.on('end', () => { entries.set(entry.fileName, Buffer.concat(chunks)); zip.readEntry(); });
      });
    });
    zip.on('end', resolve);
    zip.readEntry();
  });
  assert.equal(JSON.parse(entries.get('extension/package.json')).version, manifest.version);
  for (const file of runtimeFiles(source)) {
    const actual = entries.get('extension/kyrex_engine/' + file);
    assert.ok(actual, `Missing runtime: ${file}`);
    assert.deepEqual(actual, fs.readFileSync(path.join(source, file)), `Stale runtime: ${file}`);
  }
  assert.deepEqual(entries.get('extension/dist/extension.js'), fs.readFileSync(path.resolve(__dirname, '../dist/extension.js')), 'Stale compiled extension');
  const bundle = JSON.parse(entries.get('extension/kyrex_engine/bundle-manifest.json'));
  for (const [file, hash] of Object.entries(bundle.files)) {
    assert.equal(crypto.createHash('sha256').update(entries.get('extension/kyrex_engine/' + file)).digest('hex'), hash, `Bundle hash: ${file}`);
  }
  assert.ok(![...entries.keys()].some(name => /\/(node_modules|tests|test|__pycache__|\.px_sessions)\//.test(name)), 'Development or session files in package');
  console.log(`Verified ${filename}: version, runtime bytes, compiled extension and package exclusions.`);
})().catch(err => { console.error(err); process.exitCode = 1; });
