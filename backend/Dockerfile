FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Placeholder values only satisfy settings validation during the build.
RUN DJANGO_SECRET_KEY=build-only DATABASE_URL=sqlite:////tmp/build.sqlite3 \
    python manage.py collectstatic --noinput

USER app

EXPOSE 8000

ENTRYPOINT ["sh", "/app/entrypoint.sh"]
# Render provides PORT and WEB_CONCURRENCY; gunicorn reads WEB_CONCURRENCY itself.
# `exec` replaces the shell so gunicorn runs as PID 1 and receives SIGTERM directly.
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --access-logfile - --no-control-socket"]
