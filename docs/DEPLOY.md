# Deploying Atlas

Atlas needs four things in production: the API, at least one worker, Postgres with pgvector, and Redis. The frontend is a static-friendly Next.js app that can live anywhere.

Before any deploy:

- Set a long random `JWT_SECRET` (`python -c "import secrets;print(secrets.token_urlsafe(48))"`). The API refuses to start in `ENVIRONMENT=prod` with the default.
- Set `CORS_ORIGINS` to your frontend URL, e.g. `["https://atlas.example.com"]`.
- Set at least one model key and `MODEL_MAIN`. Gemini's free tier is enough to demo.
- Build the frontend with `NEXT_PUBLIC_API_URL` pointing at the public API URL.

## Option A: Render (blueprint included)

[`render.yaml`](../render.yaml) defines the API, worker, frontend, Postgres and Redis.

1. Push the repo to GitHub.
2. In Render: New, Blueprint, pick the repo.
3. Fill the secret env vars it asks for (`GEMINI_API_KEY`, optional `TAVILY_API_KEY`).
4. After the first deploy, set `NEXT_PUBLIC_API_URL` on `atlas-web` to the API's URL and `CORS_ORIGINS` on `atlas-api` to the web URL, then redeploy both.

Render's managed Postgres supports the `vector` extension, which Atlas enables on startup, and Atlas converts Render's `postgres://` URL to the async driver itself. The blueprint runs code with `SANDBOX_BACKEND=local` (an rlimited subprocess inside the worker container). For a public deployment switch to `SANDBOX_BACKEND=e2b` and set `E2B_API_KEY`, so user code never runs next to your secrets.

## Option B: Azure Container Apps

```bash
RG=atlas-rg; LOC=centralindia; ACR=atlasacr$RANDOM; ENV=atlas-env
az group create -n $RG -l $LOC
az acr create -g $RG -n $ACR --sku Basic --admin-enabled true
az acr build -r $ACR -t atlas-api:1 ./backend
az acr build -r $ACR -t atlas-sandbox:1 -f sandbox/Dockerfile .
az containerapp env create -g $RG -n $ENV -l $LOC

# Postgres Flexible Server (enable the extension once): az postgres flexible-server parameter set ... --name azure.extensions --value VECTOR
# Azure Cache for Redis, or a Redis container app with internal ingress

az containerapp create -g $RG -n atlas-sandbox --environment $ENV --image $ACR.azurecr.io/atlas-sandbox:1 \
  --ingress internal --target-port 8080 --env-vars SANDBOX_TOKEN=secretref:sandbox-token
az containerapp create -g $RG -n atlas-api --environment $ENV --image $ACR.azurecr.io/atlas-api:1 \
  --ingress external --target-port 8000 --min-replicas 1 \
  --env-vars ENVIRONMENT=prod QUEUE_BACKEND=arq SANDBOX_BACKEND=http SANDBOX_URL=http://atlas-sandbox \
             DATABASE_URL=secretref:db-url REDIS_URL=secretref:redis-url JWT_SECRET=secretref:jwt GEMINI_API_KEY=secretref:gemini
az containerapp create -g $RG -n atlas-worker --environment $ENV --image $ACR.azurecr.io/atlas-api:1 \
  --command arq --args app.worker.WorkerSettings --min-replicas 1 --env-vars (same as api)
```

Scale workers on queue depth with a KEDA Redis scaler if traffic grows.

## Frontend on Vercel

Import the repo, set the root directory to `frontend`, add `NEXT_PUBLIC_API_URL`, deploy. Add the Vercel URL to the API's `CORS_ORIGINS`.

## After deploying

- `GET /readyz` should return `{"status":"ready","redis":true,"database":true}`.
- Point Prometheus (or Grafana Cloud's agent) at `/metrics`. Useful panels: `atlas_run_seconds` p95, `atlas_time_to_first_token_seconds`, `rate(atlas_llm_cost_usd_total[1h])`, `atlas_tool_calls_total{status!="ok"}`, `atlas_guard_flags_total`.
- Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` for per-call traces.
- Run the live eval suite against the deployment: `python evals/run_evals.py --api https://your-api --judge-model gemini/gemini-2.5-flash`.
