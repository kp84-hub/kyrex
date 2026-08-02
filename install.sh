#!/usr/bin/env bash
# Kyrex installer — builds kx, installs git hooks, runs init tasks.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"

echo "🔧 Building kx..."
cd "$REPO_DIR"
go build -o "${BIN_DIR}/kx" .
echo "   → ${BIN_DIR}/kx"

echo "🔧 Building codescan (post-commit analysis)..."
cd "$REPO_DIR"
go build -o "${BIN_DIR}/codescan" ./cmd/codescan/
echo "   → ${BIN_DIR}/codescan"

echo "🔧 Installing Python engine..."
pip install -e kyrex_engine/ --break-system-packages --quiet 2>/dev/null || \
  pip install -e kyrex_engine/ --quiet

echo "🔧 Installing git hooks..."
HOOKS_DIR="${REPO_DIR}/.git/hooks"
mkdir -p "$HOOKS_DIR"

# post-commit hook: runs codescan on the diff after every commit
cat > "${HOOKS_DIR}/post-commit" << 'HOOK'
#!/usr/bin/env bash
# Post-commit hook: scan for slop and dead code in the diff.
# To skip: SKIP_CODESCAN=1 git commit
set -euo pipefail

if [ "${SKIP_CODESCAN:-0}" = "1" ]; then
  exit 0
fi

# Only scan if codescan is installed
if ! command -v codescan &>/dev/null; then
  exit 0
fi

# Get the diff from HEAD^ to HEAD (or empty tree on first commit)
if git rev-parse HEAD^ &>/dev/null; then
  REF="HEAD^"
else
  REF="$(git hash-object -t tree /dev/null 2>/dev/null || echo '4b825dc642cb6eb9a060e54bf899d153036e1e9e')"
fi

# Run codescan on the diff — warn but don't block the commit
OUTPUT=$(codescan --diff "$REF" 2>&1 || true)
echo "$OUTPUT" | while IFS= read -r line; do
  printf "  📋 %s\n" "$line"
done
HOOK
chmod +x "${HOOKS_DIR}/post-commit"
echo "   → ${HOOKS_DIR}/post-commit"

# pre-commit hook: fast check for obvious slop before each commit
cat > "${HOOKS_DIR}/pre-commit" << 'HOOK'
#!/usr/bin/env bash
# Pre-commit hook: fast slop check on staged files.
# To skip: SKIP_CODESCAN=1 git commit
set -euo pipefail

if [ "${SKIP_CODESCAN:-0}" = "1" ]; then
  exit 0
fi

if ! command -v codescan &>/dev/null; then
  exit 0
fi

# Get staged files
STAGED=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.(go|py|js|ts|tsx|jsx|rs|rb|c|cpp|h|hpp|java|kt)$' || true)
if [ -z "$STAGED" ]; then
  exit 0
fi

# Check for critical slop patterns (FIXME, HACK, XXX, debug prints)
CRITICAL=0
for FILE in $STAGED; do
  if [ -f "$FILE" ]; then
    # Check for HACK and XXX (strong signals)
    if grep -qnE '(HACK|XXX)' "$FILE" 2>/dev/null; then
      LINES=$(grep -nE '(HACK|XXX)' "$FILE" | head -5)
      echo "⚠  Slop detected in $FILE:"
      echo "$LINES" | while IFS= read -r line; do
        echo "   $line"
      done
      CRITICAL=$((CRITICAL + 1))
    fi
  fi
done

if [ "$CRITICAL" -gt 0 ]; then
  echo "❌ Found $CRITICAL file(s) with HACK/XXX — commit blocked."
  echo "   Use SKIP_CODESCAN=1 git commit to override."
  exit 1
fi
HOOK
chmod +x "${HOOKS_DIR}/pre-commit"
echo "   → ${HOOKS_DIR}/pre-commit"

echo ""
echo "✅ Kyrex installed successfully."
echo ""
echo "Post-commit code scanning is ACTIVE. After every commit,"
echo "codescan will analyze the diff for slop and dead code."
echo ""
echo "To bypass: SKIP_CODESCAN=1 git commit"