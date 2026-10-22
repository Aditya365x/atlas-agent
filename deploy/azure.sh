#!/usr/bin/env bash
# One-command deploy of Atlas to Azure Container Apps.
#
#   OPENROUTER_API_KEY=... ./deploy/azure.sh        # first deploy with OpenRouter (Inception Mercury models)
#   GEMINI_API_KEY=... ./deploy/azure.sh            # ...or with Google Gemini (creates everything, ~15 min)
#   ./deploy/azure.sh                               # redeploy new code (rebuilds images, rolls out)
#
# Topology (cheap enough for a student subscription, still production-shaped):
#   atlas-api  one Container App with three containers sharing localhost:
#              api (FastAPI, external ingress)  +  worker (arq)  +  redis (queue, streams, rate limits)
#   atlas-web  Next.js UI (external ingress)
#   Postgres   Azure Database for PostgreSQL Flexible Server (B1ms) with pgvector
# Images are built in the cloud with `az acr build`, so no local Docker is needed.
main() {
set -euo pipefail
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1  # az on Windows crashes printing UTF-8 build logs otherwise
cd "$(dirname "$0")/.."

RG=${RG:-atlas-rg}
LOCATION=${LOCATION:-centralindia}
ENV_NAME=${ENV_NAME:-atlas-env}
PG_LOCATION=${PG_LOCATION:-$LOCATION}
MODEL_MAIN=${MODEL_MAIN:-}          # defaults depend on which provider key is available (see below)
MODEL_PLANNER=${MODEL_PLANNER:-}
MODEL_FALLBACKS=${MODEL_FALLBACKS:-}
EMBEDDING_MODEL=${EMBEDDING_MODEL:-}
TAG=${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%s)-$(date +%H%M%S)}
STATE=deploy/.azure-state   # names + generated secrets, git-ignored

[ -f "$STATE" ] && . "$STATE"
SUFFIX=${SUFFIX:-$(openssl rand -hex 3)}
ACR=${ACR:-atlasacr$SUFFIX}
PG=${PG:-atlas-pg-$SUFFIX}
PG_PASS=${PG_PASS:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)Aa1}
JWT_SECRET=${JWT_SECRET:-$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 64)}
cat > "$STATE" <<EOF
SUFFIX=$SUFFIX
ACR=$ACR
PG=$PG
PG_PASS=$PG_PASS
JWT_SECRET=$JWT_SECRET
EOF
chmod 600 "$STATE"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "Providers and extension"
az extension add -n containerapp --upgrade -y -o none 2>/dev/null || true
for p in Microsoft.App Microsoft.ContainerRegistry Microsoft.DBforPostgreSQL Microsoft.OperationalInsights; do
  az provider register -n "$p" -o none
done

step "Resource group $RG ($LOCATION)"
az group create -n "$RG" -l "$LOCATION" -o none

step "Container registry $ACR"
az acr show -n "$ACR" -g "$RG" -o none 2>/dev/null || az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled true -o none
ACR_SERVER=$(az acr show -n "$ACR" -g "$RG" --query loginServer -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)

step "Postgres $PG (pgvector)"
if ! az postgres flexible-server show -g "$RG" -n "$PG" -o none 2>/dev/null; then
  az postgres flexible-server create -g "$RG" -n "$PG" -l "$PG_LOCATION" --tier Burstable --sku-name Standard_B1ms \
    --storage-size 32 --version 16 --admin-user atlas --admin-password "$PG_PASS" --public-access 0.0.0.0 --yes -o none
fi
az postgres flexible-server db show -g "$RG" -s "$PG" -d atlas -o none 2>/dev/null || az postgres flexible-server db create -g "$RG" -s "$PG" -d atlas -o none
az postgres flexible-server parameter set -g "$RG" -s "$PG" --name azure.extensions --value VECTOR -o none
PG_HOST=$(az postgres flexible-server show -g "$RG" -n "$PG" --query fullyQualifiedDomainName -o tsv)
DATABASE_URL="postgresql+asyncpg://atlas:${PG_PASS}@${PG_HOST}:5432/atlas?ssl=require"

step "Container Apps environment $ENV_NAME"
az containerapp env show -g "$RG" -n "$ENV_NAME" -o none 2>/dev/null || az containerapp env create -g "$RG" -n "$ENV_NAME" -l "$LOCATION" --environment-mode WorkloadProfiles -o none
# (Express environments, the CLI default since 2026, do not allow the sidecar containers used below.)
DOMAIN=$(az containerapp env show -g "$RG" -n "$ENV_NAME" --query properties.defaultDomain -o tsv)
API_URL="https://atlas-api.$DOMAIN"
WEB_URL="https://atlas-web.$DOMAIN"

step "Build images ($TAG) in ACR"
# Queue the cloud build and poll it: streaming build logs crashes az on Windows consoles (cp1252), and polling
# also works unattended in CI.
acr_build() {
  local before run status
  before=$(az acr task list-runs -r "$ACR" --top 1 --query "[0].runId" -o tsv 2>/dev/null || true)
  az acr build -r "$ACR" --no-wait --no-logs "$@" -o none
  for _ in $(seq 1 30); do
    run=$(az acr task list-runs -r "$ACR" --top 1 --query "[0].runId" -o tsv)
    [ -n "$run" ] && [ "$run" != "$before" ] && break
    sleep 4
  done
  echo "  run $run: $*"
  while :; do
    status=$(az acr task show-run -r "$ACR" --run-id "$run" --query status -o tsv)
    case "$status" in
      Succeeded) echo "  run $run: Succeeded"; return 0 ;;
      Failed|Canceled|Error|Timeout) echo "ERROR: ACR run $run $status (az acr task logs -r $ACR --run-id $run)"; return 1 ;;
    esac
    sleep 10
  done
}
if [ -n "${SKIP_BUILD:-}" ]; then  # config-only change (e.g. adding a key): reuse the newest images, ~2 min
  TAG=$(az acr repository show-tags -n "$ACR" --repository atlas-api --orderby time_desc --top 1 -o tsv)
  echo "  SKIP_BUILD: reusing images $TAG"
else
  acr_build -t "atlas-api:$TAG" ./backend
  acr_build -t "atlas-web:$TAG" --build-arg "NEXT_PUBLIC_API_URL=$API_URL" ./frontend
fi

step "Deploy atlas-api (api + worker + redis)"
stored_secret() {  # value of an existing Container Apps secret, empty if missing or a placeholder
  local v
  v=$(az containerapp secret show -g "$RG" -n atlas-api --secret-name "$1" --query value -o tsv 2>/dev/null || true)
  [ "$v" = "unset" ] && v=""
  printf '%s' "$v"
}
GEMINI_API_KEY=${GEMINI_API_KEY:-$(stored_secret gemini-key)}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-$(stored_secret openrouter-key)}
if [ -z "$GEMINI_API_KEY$OPENROUTER_API_KEY" ] && [ -t 0 ]; then
  read -r -s -p "OpenRouter API key (input hidden; leave blank for the offline demo model): " OPENROUTER_API_KEY
  echo
fi
# Terminals wrap pasted text in bracketed-paste escape codes (ESC[200~ ... ESC[201~); strip those, control
# characters and whitespace so the key is exactly what was copied.
clean_key() { printf '%s' "$1" | sed 's/\x1b\[[0-9;]*[~A-Za-z]//g' | tr -d '[:cntrl:][:space:]'; }
GEMINI_API_KEY=$(clean_key "$GEMINI_API_KEY")
OPENROUTER_API_KEY=$(clean_key "$OPENROUTER_API_KEY")
if [ -n "$OPENROUTER_API_KEY" ]; then  # fail in seconds on a bad key, not after a 10-minute rollout
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/key)
  if [ "$code" != "200" ]; then
    echo "ERROR: OpenRouter rejected this key (HTTP $code). Check it at https://openrouter.ai/keys and run again."
    exit 1
  fi
  echo "OpenRouter key verified."
fi
deployed_env() {  # keep a model chosen with deploy/set-model.sh across redeploys
  az containerapp show -g "$RG" -n atlas-api --query "properties.template.containers[?name=='api'] | [0].env[?name=='$1'] | [0].value" -o tsv 2>/dev/null || true
}
if [ "$(deployed_env LLM_MODE)" = "live" ]; then
  MODEL_MAIN=${MODEL_MAIN:-$(deployed_env MODEL_MAIN)}
  MODEL_PLANNER=${MODEL_PLANNER:-$(deployed_env MODEL_PLANNER)}
  MODEL_FALLBACKS=${MODEL_FALLBACKS:-$(deployed_env MODEL_FALLBACKS)}
fi
if [ -n "$OPENROUTER_API_KEY" ]; then
  MODEL_MAIN=${MODEL_MAIN:-openrouter/inception/mercury-3}
  MODEL_PLANNER=${MODEL_PLANNER:-openrouter/inception/mercury-2.5}
  MODEL_FALLBACKS=${MODEL_FALLBACKS:-'["openrouter/inception/mercury-2.5"]'}
fi
MODEL_MAIN=${MODEL_MAIN:-gemini/gemini-2.5-flash}
MODEL_PLANNER=${MODEL_PLANNER:-gemini/gemini-2.5-flash-lite}
MODEL_FALLBACKS=${MODEL_FALLBACKS:-'["gemini/gemini-2.5-flash-lite"]'}
if [ -n "$GEMINI_API_KEY$OPENROUTER_API_KEY" ]; then
  LLM_MODE=${LLM_MODE:-live}
  echo "Live model: $MODEL_MAIN (planner $MODEL_PLANNER)"
else
  echo "No model key: deploying with the offline demo model. Re-run with OPENROUTER_API_KEY=... or GEMINI_API_KEY=... to go live."
  LLM_MODE=fake
fi
GEMINI_API_KEY=${GEMINI_API_KEY:-unset}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-unset}
TAVILY_API_KEY=${TAVILY_API_KEY:-$(az containerapp secret show -g "$RG" -n atlas-api --secret-name tavily-key --query value -o tsv 2>/dev/null || true)}

ENV_COMMON=$(cat <<EOF
          - {name: ENVIRONMENT, value: prod}
          - {name: LLM_MODE, value: "$LLM_MODE"}
          - {name: QUEUE_BACKEND, value: arq}
          - {name: REDIS_URL, value: "redis://localhost:6379/0"}
          - {name: SANDBOX_BACKEND, value: local}
          - {name: MODEL_MAIN, value: "$MODEL_MAIN"}
          - {name: MODEL_PLANNER, value: "$MODEL_PLANNER"}
          - {name: MODEL_FALLBACKS, value: '$MODEL_FALLBACKS'}
          - {name: EMBEDDING_MODEL, value: "$EMBEDDING_MODEL"}
          - {name: CORS_ORIGINS, value: '["$WEB_URL"]'}
          - {name: DAILY_USER_COST_USD, value: "1.0"}
          - {name: GUEST_DAILY_COST_USD, value: "0.15"}
          - {name: GLOBAL_DAILY_COST_USD, value: "5"}
          - {name: RATE_LIMIT_PER_MINUTE, value: "12"}
          - {name: DB_POOL_SIZE, value: "3"}
          - {name: DB_MAX_OVERFLOW, value: "3"}
          - {name: DATABASE_URL, secretRef: db-url}
          - {name: JWT_SECRET, secretRef: jwt-secret}
          - {name: GEMINI_API_KEY, secretRef: gemini-key}
          - {name: OPENROUTER_API_KEY, secretRef: openrouter-key}
EOF
)
TAVILY_SECRET=""
if [ -n "${TAVILY_API_KEY:-}" ]; then
  ENV_COMMON="$ENV_COMMON
          - {name: TAVILY_API_KEY, secretRef: tavily-key}"
  TAVILY_SECRET="      - {name: tavily-key, value: \"$TAVILY_API_KEY\"}"
fi
trap 'rm -f deploy/.atlas-api.yaml' EXIT  # holds secrets: never leave it behind, even on failure
umask 077
cat > deploy/.atlas-api.yaml <<EOF
location: $LOCATION
properties:
  managedEnvironmentId: $(az containerapp env show -g "$RG" -n "$ENV_NAME" --query id -o tsv)
  configuration:
    activeRevisionsMode: Single
    ingress: {external: true, targetPort: 8000, transport: auto, allowInsecure: false}
    registries: [{server: $ACR_SERVER, username: $ACR, passwordSecretRef: acr-pass}]
    secrets:
      - {name: acr-pass, value: "$ACR_PASS"}
      - {name: db-url, value: "$DATABASE_URL"}
      - {name: jwt-secret, value: "$JWT_SECRET"}
      - {name: gemini-key, value: "$GEMINI_API_KEY"}
      - {name: openrouter-key, value: "$OPENROUTER_API_KEY"}
$TAVILY_SECRET
  template:
    scale: {minReplicas: 1, maxReplicas: 1}
    containers:
      - name: redis
        image: docker.io/library/redis:7-alpine
        command: ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "200mb", "--maxmemory-policy", "noeviction"]
        resources: {cpu: 0.25, memory: 0.5Gi}
      - name: api
        image: $ACR_SERVER/atlas-api:$TAG
        resources: {cpu: 0.5, memory: 1Gi}
        env:
          - {name: WEB_CONCURRENCY, value: "2"}
$ENV_COMMON
        probes:
          - {type: Startup, httpGet: {path: /healthz, port: 8000}, initialDelaySeconds: 5, periodSeconds: 5, failureThreshold: 30}
          - {type: Liveness, httpGet: {path: /healthz, port: 8000}, periodSeconds: 20}
          - {type: Readiness, httpGet: {path: /readyz, port: 8000}, periodSeconds: 10}
      - name: worker
        image: $ACR_SERVER/atlas-api:$TAG
        command: ["arq", "app.worker.WorkerSettings"]
        resources: {cpu: 0.75, memory: 1.5Gi}
        env:
$ENV_COMMON
EOF
if az containerapp show -g "$RG" -n atlas-api -o none 2>/dev/null; then
  az containerapp update -g "$RG" -n atlas-api --yaml deploy/.atlas-api.yaml -o none
else
  az containerapp create -g "$RG" -n atlas-api --yaml deploy/.atlas-api.yaml -o none
fi
rm -f deploy/.atlas-api.yaml

step "Deploy atlas-web"
if az containerapp show -g "$RG" -n atlas-web -o none 2>/dev/null; then
  az containerapp update -g "$RG" -n atlas-web --image "$ACR_SERVER/atlas-web:$TAG" -o none
else
  az containerapp create -g "$RG" -n atlas-web --environment "$ENV_NAME" --image "$ACR_SERVER/atlas-web:$TAG" \
    --registry-server "$ACR_SERVER" --registry-username "$ACR" --registry-password "$ACR_PASS" \
    --ingress external --target-port 3000 --min-replicas 1 --max-replicas 2 --cpu 0.25 --memory 0.5Gi -o none
fi

step "Smoke test"
for i in $(seq 1 40); do
  if curl -fsS "$API_URL/readyz" >/dev/null 2>&1; then break; fi
  sleep 6
done
curl -fsS "$API_URL/readyz" && echo
curl -fsS -o /dev/null -w "web: HTTP %{http_code}\n" "$WEB_URL"
printf '\nAtlas is live\n  App: %s\n  API: %s/docs\n  MCP: %s/mcp\n' "$WEB_URL" "$API_URL" "$API_URL"
}
main "$@"
