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
backend/    Django project (config/) and apps (apps/core: health check)
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
