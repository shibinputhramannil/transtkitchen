# KitchenMind AI

Kitchen operations for independent restaurants, bakeries, and cloud kitchens:
sales-based demand forecasting, an editable prep planner, inventory and expiry
tracking, and waste logging.

> **Status: Phase 1 (foundation).** The stack runs end to end with a health
> check. Authentication and product features start in Phase 2.

## Stack

| Layer    | Technology                                      |
| -------- | ----------------------------------------------- |
| Frontend | React 19 + Vite, plain JavaScript, oxlint       |
| API      | Django 5.2 LTS + Django REST Framework          |
| Database | PostgreSQL 17                                   |
| Local    | Docker Compose                                  |
| Hosting  | Render: static site + Docker web service + Postgres |

```
backend/    Django project (config/) and apps (apps/accounts, apps/core)
frontend/   Vite React app
docker-compose.yml   local development stack
render.yaml          Render Blueprint for production
```

## Run locally

Requires Docker Desktop (running).

```sh
docker compose up --build
```

- Frontend: http://localhost:5173 (shows API and database status)
- API health: http://localhost:8000/api/health/
- Django admin: http://localhost:8000/admin/ (create a user with
  `docker compose exec backend python manage.py createsuperuser`)

The backend runs database migrations on startup. Code changes in `backend/`
and `frontend/` reload automatically.

If port 8000 is already in use, pick another host port for the API:

```sh
BACKEND_PORT=8010 docker compose up --build
```

```powershell
$env:BACKEND_PORT = "8010"; docker compose up --build
```

The API is then at http://localhost:8010 and the frontend automatically calls
that port. The frontend stays on http://localhost:5173, which is the origin the
API allows for CORS and CSRF, so nothing else needs to change. You can also put
`BACKEND_PORT=8010` in a root `.env` file (git-ignored) so Compose picks it up
every time.

Stop with `Ctrl+C`; `docker compose down -v` also deletes the local database.

## Tests and checks

```sh
docker compose exec backend python manage.py test
docker compose exec frontend npm run lint
docker compose exec frontend npm run build
```

Backend tests need PostgreSQL, so run them through Docker Compose as above
rather than with a local Python install.

## Backend

```
backend/
  config/          settings, URLs, WSGI/ASGI, env.py (environment helpers), tests.py
  apps/accounts/   custom User model (AUTH_USER_MODEL = "accounts.User")
  apps/core/       health check, TimeStampedModel
```

- **Settings** live in one file, `config/settings.py`, and every
  environment-specific value comes from an environment variable read through
  `config/env.py`.
- **Users:** always refer to the user model with `settings.AUTH_USER_MODEL` (in
  model fields) or `get_user_model()` (in code), never by importing Django's
  default `User`.
- **Timestamps:** models that need `created_at` / `updated_at` inherit from
  `apps.core.models.TimeStampedModel`.
- **API defaults:** endpoints require authentication unless they set
  `permission_classes` explicitly, and list endpoints are paginated 50 per page.

### Adding an app

```sh
docker compose exec backend sh -c "mkdir apps/<name> && python manage.py startapp <name> apps/<name>"
```

Then in `apps/<name>/apps.py` set `name = "apps.<name>"`, add `"apps.<name>"`
to `INSTALLED_APPS`, and create migrations with
`docker compose exec backend python manage.py makemigrations <name>`.

## Configuration

Backend variables are documented in [backend/.env.example](backend/.env.example)
and frontend variables in [frontend/.env.example](frontend/.env.example).
Docker Compose already sets development values. Never commit a real `.env` file.

Security defaults:

- `DJANGO_DEBUG` is off unless explicitly enabled.
- The API refuses to start without `DJANGO_SECRET_KEY` and `DATABASE_URL`.
- CORS allows only the origins listed in `CORS_ALLOWED_ORIGINS`, without credentials.
- API endpoints deny access by default; the health check is the only public endpoint.
- In production, HTTPS redirects, secure cookies, and HSTS are enabled.

## Deploy to Render

1. Push this repository to GitHub or GitLab.
2. In Render, choose **New → Blueprint** and select the repository. Render reads
   `render.yaml` and creates `kitchenmind-db`, `kitchenmind-api`, and `kitchenmind-web`.
3. When prompted, enter:
   - `kitchenmind-api` → `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS`: the
     frontend URL, e.g. `https://kitchenmind-web.onrender.com`
   - `kitchenmind-web` → `VITE_API_URL`: the API URL, e.g.
     `https://kitchenmind-api.onrender.com`

   Render may add a suffix to service names that are already taken; use the
   URLs shown in the dashboard. If you change `VITE_API_URL` later, redeploy
   the static site, because the value is built into the bundle.
4. Open the frontend URL and confirm both statuses show `ok`.

Free plan limits: the free Postgres database expires 30 days after creation
(deleted after a further 14-day grace period), and the free API service sleeps
after 15 minutes without traffic (the first request then takes about a minute).
Upgrade both before storing real data.
