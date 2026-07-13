# Explicit Dockerfile so the deploy is builder-independent: Nixpacks/Railpack
# auto-detection was leaving the venv off PATH at runtime (gunicorn not found).
FROM python:3.13-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# Presence state lives in a SQLite file here. Without a volume it is ephemeral
# (rebuilt from agents' next heartbeats after a restart); mount a volume at
# /var/lib/standup to make the board survive restarts.
RUN mkdir -p /var/lib/standup
ENV STANDUP_DB_PATH=/var/lib/standup/standup.db

# Railway injects $PORT at runtime; shell form expands it.
CMD ["sh", "-c", "gunicorn standup_board.wsgi:app --workers 1 --bind 0.0.0.0:${PORT:-8080}"]
