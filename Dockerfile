FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .
COPY gsc_mcp/ gsc_mcp/

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Cloud Run sets $PORT. --proxy-headers is not enabled: client IPs are not used for any decision.
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --no-server-header
