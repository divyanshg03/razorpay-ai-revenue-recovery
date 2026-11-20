# The hosted webhook receiver. Stdlib only, so the image is the interpreter plus src/.
# Configuration comes from the environment at run time; see src/recovery/ingest/server.py.
FROM python:3.13-slim

WORKDIR /app
COPY src/ ./src/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    EVENT_STORE=/app/runtime/events.sqlite3

# Never run as root: the process parses bodies from the open internet.
RUN useradd --create-home --uid 10001 app && mkdir -p /app/runtime && chown app /app/runtime
USER app

EXPOSE 8080
CMD ["python", "-m", "recovery.ingest.server"]
