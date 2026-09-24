#!/usr/bin/env bash
# 冒烟：起真服务，只打零消耗端点（health/models/dry-run）—— 不触上游。
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${SMOKE_PORT:-8127}
PY=${PYTHON:-python3}

METASO_ENABLED=1 HOST=127.0.0.1 PORT=$PORT $PY -m uvicorn app.main:app \
  --host 127.0.0.1 --port $PORT --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  sleep 0.5
done

echo "== /health =="
curl -sf "http://127.0.0.1:$PORT/health" | $PY -c "import json,sys;d=json.load(sys.stdin);print(json.dumps({'ready':d['ready'],'egress':d['upstream']['egress']},ensure_ascii=False))"
echo "== /v1/models =="
curl -sf "http://127.0.0.1:$PORT/v1/models" | $PY -c "import json,sys;print(len(json.load(sys.stdin)['data']),'models')"
echo "== dry-run search（零上游消耗）=="
curl -sf -X POST "http://127.0.0.1:$PORT/v1/chat/completions" \
  -H 'content-type: application/json' \
  -d '{"model":"metaso:search","dry_run":true,"messages":[{"role":"user","content":"冒烟"}]}' \
  | $PY -c "import json,sys;d=json.load(sys.stdin);assert d['dry_run'] is True;print('dry_run ok, token_source =',d['effective']['upstream_request']['_token_source'])"
echo "SMOKE PASS"
