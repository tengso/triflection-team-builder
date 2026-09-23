#!/usr/bin/env bash
# No operational databases or application credentials are used.
set -euo pipefail
image=${1:?image required}
ui="hti-smoke-ui-$$"
api="hti-smoke-api-$$"
trap 'docker rm -f "$ui" "$api" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$ui" "$image" ui >/dev/null
docker run -d --name "$api" -e CRM_REST_TOKEN=ci-disposable-token "$image" python -m uvicorn modules.research.crm.data_api_main:app --host 0.0.0.0 --port 8002 >/dev/null
for spec in "$ui:8502:/_stcore/health" "$api:8002:/health"; do
  IFS=: read -r container port path <<< "$spec"
  ready=false
  for attempt in {1..60}; do
    if docker exec "$container" python -c 'import sys,urllib.request; r=urllib.request.Request("http://127.0.0.1:"+sys.argv[1]+sys.argv[2], headers={"Authorization":"Bearer ci-disposable-token"}); assert urllib.request.urlopen(r, timeout=2).status==200' "$port" "$path" >/dev/null 2>&1; then
      ready=true; break
    fi
    sleep 2
  done
  if [[ "$ready" != true ]]; then docker logs "$container"; exit 1; fi
done
