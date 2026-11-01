#!/bin/sh
set -e

# Render's free plan has no pre-deploy command, so migrations run at startup.
python manage.py migrate --noinput

exec "$@"
