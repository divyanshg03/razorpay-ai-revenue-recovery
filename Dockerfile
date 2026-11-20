# The hosted webhook receiver: the interpreter, src/, and one pinned pure-Python driver.
# Configuration comes from the environment at run time; see src/recovery/ingest/server.py.
FROM python:3.13-slim

WORKDIR /app

# Postgres driver for pg_store.py, used when DATABASE_URL is set. Installed only here:
# the repository itself still has no runtime dependencies. Pinned, so a rebuild cannot
# silently pick up a different driver.
RUN pip install --no-cache-dir pg8000==1.31.6

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
