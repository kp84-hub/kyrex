const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const source = path.resolve(__dirname, '../../kyrex_engine');
const target = path.resolve(__dirname, '../kyrex_engine');
function runtimeFiles(root, prefix = '') {
  return fs.readdirSync(path.join(root, prefix), { withFileTypes: true }).flatMap(entry => {
    const name = path.posix.join(prefix, entry.name);
    if (entry.isDirectory()) return ['__pycache__', 'tests', 'build', 'dist'].includes(entry.name) ? [] : runtimeFiles(root, name);
    return entry.isFile() && !entry.name.startsWith('test_') &&
      (name.startsWith('kyrex/') ? /\.(py|json)$/.test(name) : ['core_bridge.py', 'setup.py', 'pyproject.toml', 'README.md'].includes(name)) ? [name] : [];
  }).sort();
}
function sync() {
  fs.rmSync(target, { recursive: true, force: true });
  const files = runtimeFiles(source);
  for (const file of files) {
    fs.mkdirSync(path.dirname(path.join(target, file)), { recursive: true });
    fs.copyFileSync(path.join(source, file), path.join(target, file));
  }
  const hashes = Object.fromEntries(files.map(file => [file, crypto.createHash('sha256').update(fs.readFileSync(path.join(source, file))).digest('hex')]));
  fs.writeFileSync(path.join(target, 'bundle-manifest.json'), JSON.stringify({ files: hashes }, null, 2) + '\n');
  console.log(`Bundled ${files.length} current engine files.`);
}
if (require.main === module) sync();
module.exports = { runtimeFiles, source, target, sync };
