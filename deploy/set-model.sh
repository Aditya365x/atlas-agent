#!/usr/bin/env bash
# Switch the live deployment's model in about a minute: no rebuild, no .env, no key needed again.
#
#   ./deploy/set-model.sh --list [filter]                       # OpenRouter models that support tool calling, with prices
#   ./deploy/set-model.sh openrouter/inception/mercury-3          # main model (planner = same)
#   ./deploy/set-model.sh openrouter/openai/gpt-5-mini openrouter/inception/mercury-2.5   # main + cheaper planner
#   ./deploy/set-model.sh --show                                  # what is running now
#
# One OpenRouter key covers every openrouter/... model. Other providers (gemini/..., openai/...) need their own key
# stored on the app. The previous model becomes the automatic fallback, so a bad pick degrades instead of breaking.
set -euo pipefail
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
RG=${RG:-atlas-rg}
APP=${APP:-atlas-api}

current() {
  az containerapp show -g "$RG" -n "$APP" \
    --query "properties.template.containers[?name=='api'] | [0].env[?name=='$1'] | [0].value" -o tsv 2>/dev/null
}

catalog() { curl -fsS https://openrouter.ai/api/v1/models; }

case "${1:-}" in
  ""|-h|--help)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  --show)
    echo "main:      $(current MODEL_MAIN)"
    echo "planner:   $(current MODEL_PLANNER)"
    echo "fallbacks: $(current MODEL_FALLBACKS)"
    exit 0 ;;
  --list)
    catalog | python -c "
import json, sys
flt = sys.argv[1].lower() if len(sys.argv) > 1 else ''
rows = []
for m in json.load(sys.stdin)['data']:
    if 'tools' not in (m.get('supported_parameters') or []) or flt not in m['id'].lower():
        continue
    p = m['pricing']
    rows.append((float(p['prompt']) * 1e6, float(p['completion']) * 1e6, m['id'], m.get('context_length') or 0))
print(f\"{'model (use with openrouter/ prefix)':<52} {'in \$/M':>8} {'out \$/M':>8} {'context':>9}\")
for i, o, mid, ctx in sorted(rows):
    print(f'{mid:<52} {i:>8.3f} {o:>8.3f} {ctx:>9}')
" "${2:-}"
    exit 0 ;;
esac

MAIN=$1
PLANNER=${2:-$1}
OLD=$(current MODEL_MAIN)

# Catch typos before touching the deployment: the model must exist on OpenRouter and support tool calling.
for m in "$MAIN" "$PLANNER"; do
  if [[ "$m" == openrouter/* ]]; then
    catalog | python -c "
import json, sys
want = sys.argv[1].removeprefix('openrouter/')
models = {m['id']: m for m in json.load(sys.stdin)['data']}
if want not in models:
    close = [k for k in models if want.split('/')[-1].split('-')[0] in k][:8]
    sys.exit(f'Unknown OpenRouter model: {want}. Similar: {close or \"run --list\"}')
if 'tools' not in (models[want].get('supported_parameters') or []):
    sys.exit(f'{want} does not support tool calling, which Atlas needs. Pick another (run --list).')
" "$m"
  fi
done

FALLBACKS="[]"
[ -n "$OLD" ] && [ "$OLD" != "$MAIN" ] && FALLBACKS="[\"$OLD\"]"

echo "Switching $APP: main=$MAIN planner=$PLANNER fallback=$FALLBACKS"
# api and worker both read the model settings; one revision per container update.
for c in api worker; do
  az containerapp update -g "$RG" -n "$APP" --container-name "$c" \
    --set-env-vars "MODEL_MAIN=$MAIN" "MODEL_PLANNER=$PLANNER" "MODEL_FALLBACKS=$FALLBACKS" -o none
done

FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)
for _ in $(seq 1 30); do
  model=$(curl -fsS "https://$FQDN/v1/info" 2>/dev/null | python -c "import json,sys;print(json.load(sys.stdin)['model'])" 2>/dev/null || true)
  [ "$model" = "$MAIN" ] && { echo "Live: $model"; exit 0; }
  sleep 6
done
echo "Updated, but /v1/info still reports '${model:-unreachable}'. Check: az containerapp revision list -g $RG -n $APP -o table"
