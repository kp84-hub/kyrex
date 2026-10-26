#!/bin/sh
# Run inside the final developer toolchain image, without network or credentials.
set -eu
workspace=$(mktemp -d)
trap 'rm -rf "$workspace"' EXIT
cd "$workspace"
python3 -m pytest --version
node --version
npm --version
go version
cat > test_runtime.py <<'PY'
def test_python_runtime():
    import asyncio
    assert asyncio.run(asyncio.sleep(0, result=42)) == 42
PY
python3 -m pytest -q test_runtime.py
cat > package.json <<'JSON'
{"private":true,"type":"module","scripts":{"test":"node --test test_runtime.mjs"}}
JSON
cat > test_runtime.mjs <<'JS'
import test from 'node:test';
import assert from 'node:assert/strict';
test('Node and npm execute ES modules', () => assert.equal(21 * 2, 42));
JS
npm test
cat > go.mod <<'GO'
module runtimecheck
go 1.24.0
GO
cat > runtime_test.go <<'GO'
package runtimecheck
import "testing"
func TestCompiler(t *testing.T) {
    if 21 * 2 != 42 { t.Fatal("unexpected result") }
}
GO
GOTOOLCHAIN=local go test ./...
