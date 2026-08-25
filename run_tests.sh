#!/bin/bash
# Baseline/test runner for kyrex-cloud tests.
# Usage: run_tests.sh [outdir]
cd /tmp/kyrex-task-agent-1787683314-kyrex-cloud-implement-milestone-1-the/kyrex-cloud
OUTDIR="${1:-/tmp/cloud_test_results}"
mkdir -p "$OUTDIR"
SUMMARY="$OUTDIR/SUMMARY.txt"
: > "$SUMMARY"
for f in test_*.py; do
  start=$(date +%s)
  if timeout 120 python3 "$f" > "$OUTDIR/$f.out" 2>&1; then
    exitcode=0
  else
    exitcode=$?
  fi
  end=$(date +%s)
  dur=$((end-start))
  if grep -q "ALL TESTS PASSED" "$OUTDIR/$f.out"; then
    status="PASS"
  else
    status="FAIL"
  fi
  echo "$f exit=$exitcode $status (${dur}s)" >> "$SUMMARY"
done
echo "RUN_COMPLETE" >> "$SUMMARY"
