.PHONY: up down dev-api dev-web test test-int lint evals evals-live load

up:            ## full stack in Docker
	docker compose up --build
down:
	docker compose down
dev-api:       ## API on SQLite with the offline model
	cd backend && LLM_MODE=fake uvicorn app.main:app --reload
dev-web:
	cd frontend && npm run dev
lint:
	cd backend && ruff check app tests && ruff format --check app tests
	cd frontend && npm run lint
test:
	cd backend && pytest -q
test-int:      ## needs Postgres (pgvector) + Redis running
	cd backend && TEST_DATABASE_URL=postgresql+asyncpg://atlas:atlas@localhost:5432/atlas TEST_REDIS_URL=redis://localhost:6379/0 pytest -q
evals:         ## against a running API in LLM_MODE=fake
	python evals/run_evals.py --offline --min-pass 1.0
evals-live:
	python evals/run_evals.py --judge-model $${MODEL_MAIN:-gemini/gemini-2.5-flash}
load:
	k6 run -e API=http://localhost:8000 loadtest/k6.js
