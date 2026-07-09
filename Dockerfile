FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
COPY adapters ./adapters
COPY automation ./automation
# evals/ ships the MCP connection helper the work loop reuses (and lets
# self-hosters run the agent evaluation inside the container).
COPY evals ./evals

# Non-root user; /data holds the SQLite volume in compose deployments,
# automation/logs receives daily digests.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data automation/logs \
    && chown -R appuser /data /app/automation/logs
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
