#!/bin/sh
set -eu
# The stable workflow ID and persistent checksum prevent duplicate imports on restart.
checksum=$(sha256sum /config/workflow.json | cut -d ' ' -f 1)
marker=/home/node/.n8n/order-review-workflow.sha256
if [ ! -f "$marker" ] || [ "$(cat "$marker")" != "$checksum" ]; then
  n8n import:workflow --input=/config/workflow.json
  node /config/ensure-history.cjs
  n8n publish:workflow --id=schaltschrankReview01
  n8n export:workflow --id=schaltschrankReview01 --output=/tmp/order-review-verified.json
  node -e 'const w=require("/tmp/order-review-verified.json")[0]; if(!w || !w.active || !w.activeVersionId) { console.error("Workflow publication verification failed"); process.exit(1); }'
  printf '%s' "$checksum" > "$marker"
fi
exec n8n start
