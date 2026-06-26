#!/usr/bin/env bash
# Run Claude Code as a baseline and capture process + result separately.
#
# Usage:
#   ./run_claude_baseline.sh <prompt_file> <output_dir>
#
# Example:
#   ./run_claude_baseline.sh prompts/user_management.txt user/claude/
#
# Outputs:
#   <output_dir>/process.jsonl   — one JSON event per line (stream)
#   <output_dir>/result.json     — final result with tokens, cost, duration

set -e

PROMPT_FILE="${1:?Usage: $0 <prompt_file> <output_dir>}"
OUT_DIR="${2:?Usage: $0 <prompt_file> <output_dir>}"

if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "Error: prompt file '$PROMPT_FILE' not found" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

echo "[run] prompt: $PROMPT_FILE"
echo "[run] output: $OUT_DIR"

export _START_MS=$(python3 -c "import time; print(int(time.time() * 1000))")

claude -p "$(cat "$PROMPT_FILE")" \
    --model claude-sonnet-4-6 \
    --output-format stream-json \
    --verbose \
    --dangerously-skip-permissions \
    | tee "$OUT_DIR/process.jsonl" \
    | python3 -u -c "
import sys, json, os, time

start_ms = int(os.environ['_START_MS'])
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    t = obj.get('type')
    if t == 'assistant':
        for block in obj.get('message', {}).get('content', []):
            bt = block.get('type')
            if bt == 'text':
                print(block.get('text', ''), end='', flush=True, file=sys.stderr)
            elif bt == 'tool_use':
                print(f\"\n[→ {block.get('name','?')}]\", flush=True, file=sys.stderr)
    elif t == 'system' and obj.get('subtype') == 'task_progress':
        desc = obj.get('description', '')
        if desc:
            print(f\"[task] {desc}\", flush=True, file=sys.stderr)
    elif t == 'result':
        obj['wall_time_ms'] = int(time.time() * 1000) - start_ms
        u = obj.get('usage', {})
        print(f\"\n[usage] in={u.get('input_tokens',0):,}  out={u.get('output_tokens',0):,}  cost=\${obj.get('cost_usd',0):.4f}  wall={obj['wall_time_ms']/1000:.1f}s\", flush=True, file=sys.stderr)
        print(json.dumps(obj, indent=2))
" > "$OUT_DIR/result.json"

unset _START_MS

echo "[done] result  -> $OUT_DIR/result.json"
echo "[done] process -> $OUT_DIR/process.jsonl"
