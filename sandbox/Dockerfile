# Build context: repository root (needs backend/app/sandbox_runner.py)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.32.*" numpy pandas matplotlib scipy \
    && useradd --create-home --uid 10001 sandbox
WORKDIR /srv
COPY backend/app/sandbox_runner.py sandbox/server.py ./
# warm the matplotlib font cache at build time so first chart is fast
RUN MPLCONFIGDIR=/tmp/mpl python -c "import matplotlib.pyplot" && rm -rf /tmp/mpl
USER sandbox
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
