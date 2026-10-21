# ---- build stage: resolve wheels once ----
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip wheel --wheel-dir /wheels .

# ---- runtime stage: small, non-root ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 atlas
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
WORKDIR /app
COPY app ./app
USER atlas
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"
# Role is chosen at runtime: API (default) or worker (`arq app.worker.WorkerSettings`)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='*'"]
