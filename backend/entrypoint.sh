#!/bin/sh
set -e

# Render's free plan has no pre-deploy command, so these run at startup.
python manage.py migrate --noinput
# Revoked and expired sign-in tokens pile up; a free service restarts often
# (after every spin-down), so cleaning up here keeps the table small.
python manage.py flushexpiredtokens
# Creates the first admin account only when DJANGO_SUPERUSER_EMAIL and
# DJANGO_SUPERUSER_PASSWORD are set; otherwise does nothing.
python manage.py bootstrap_admin

exec "$@"
