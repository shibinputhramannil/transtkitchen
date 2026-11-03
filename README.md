# KitchenMind AI

Kitchen operations for independent restaurants, bakeries, and cloud kitchens:
sales-based demand forecasting, an editable prep planner, inventory and expiry
tracking, and waste logging.

> **Status: Phase 4b (expiry tracking).** People can register, sign in, create
> an organization, manage its ingredients, record deliveries with expiry dates
> and stock counts, and see what is expired or expiring. Sales imports and
> waste logging come next.

## Stack

| Layer    | Technology                                      |
| -------- | ----------------------------------------------- |
| Frontend | React 19 + Vite, plain JavaScript, oxlint       |
| API      | Django 5.2 LTS + Django REST Framework          |
| Database | PostgreSQL 17                                   |
| Local    | Docker Compose                                  |
| Hosting  | Render: static site + Docker web service + Postgres |

```
backend/    Django project (config/) and apps (accounts, core, organizations, inventory)
frontend/   Vite React app
docker-compose.yml   local development stack
render.yaml          Render Blueprint for production
```

## Run locally

Requires Docker Desktop (running).

```sh
docker compose up --build
```

- Frontend: http://localhost:5173 (create an account there to sign in)
- API health: http://localhost:8000/api/health/
- Django admin: http://localhost:8000/admin/ (create an admin with
  `docker compose exec backend python manage.py createsuperuser`; it asks for
  an email address, not a username)

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
  config/               settings, URLs, WSGI/ASGI, env.py (environment helpers), tests.py
  apps/accounts/        User model (sign-in by email), registration, JWT sign-in
  apps/core/            health check, TimeStampedModel, testing.py (API test base)
  apps/organizations/   tenants: organizations, memberships and roles, invitations,
                        testing.py (two-tenant test fixtures)
  apps/inventory/       ingredients, stock batches and expiry, stock history;
                        stock.py holds the rules every stock change follows
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

## Authentication and tenants

People sign in with their email address and password. The API returns a
short-lived **access token** (15 minutes) and a **refresh token** (7 days),
sent as `Authorization: Bearer <access token>`. Each refresh returns a new
refresh token and revokes the old one; signing out and changing the password
revoke them too.

The frontend keeps both tokens **in memory only**, never in localStorage,
sessionStorage, or cookies. **Reloading or closing the tab signs you out**,
by design for this phase.

Each **organization** is one tenant (a restaurant, bakery, or cloud kitchen).
People belong to organizations through **memberships**, each with a role:

| Action                              | Owner    | Manager      | Staff     |
| ----------------------------------- | -------- | ------------ | --------- |
| See the organization and members    | yes      | yes          | yes       |
| Rename the organization             | yes      | no           | no        |
| Invite people, revoke invitations   | any role | staff only   | no        |
| Change roles                        | yes      | no           | no        |
| Remove members                      | anyone   | staff only   | only self |

An organization always keeps at least one owner. Someone who is not a member
gets `404 Not Found`, as if the organization did not exist; a member whose role
is too low gets `403 Forbidden`.

**Invitations** are addressed to an email address and look the same whether or
not that address has an account, so inviting never reveals who is registered.
The invitee signs in (or registers) with that address and accepts or declines;
invitations expire after 7 days. There is no email sending yet, so tell people
about their invitation yourself, and accepting is API-only until the member
management screens are built.

### API

| Endpoint                                              | Methods          |
| ----------------------------------------------------- | ---------------- |
| `/api/auth/register/`                                 | POST             |
| `/api/auth/token/` (sign in)                          | POST             |
| `/api/auth/token/refresh/`                            | POST             |
| `/api/auth/logout/`                                   | POST             |
| `/api/auth/me/`                                       | GET, PATCH       |
| `/api/organizations/`                                 | GET, POST        |
| `/api/organizations/<id>/`                            | GET, PATCH       |
| `/api/organizations/<id>/members/`                    | GET              |
| `/api/organizations/<id>/members/<membership id>/`    | GET, PATCH, DELETE |
| `/api/organizations/<id>/invitations/`                | GET, POST        |
| `/api/organizations/<id>/invitations/<invitation id>/`| DELETE           |
| `/api/invitations/` (your pending invitations)        | GET              |
| `/api/invitations/<id>/accept/`, `.../decline/`       | POST             |

Registration and sign-in are limited to 10 requests per minute per client.

### Adding tenant data

Every view under `/api/organizations/<organization_id>/` inherits
`apps.organizations.mixins.OrganizationScopedMixin`. It resolves the
organization through the requester's own membership, checks `minimum_role`,
and exposes `self.organization` and `self.membership`. Build querysets from
`self.organization` and set it on new objects in the view, never from request
data:

```python
class WasteEntryListCreateView(OrganizationScopedMixin, generics.ListCreateAPIView):
    serializer_class = WasteEntrySerializer
    minimum_role = Role.STAFF

    def get_queryset(self):
        return WasteEntry.objects.filter(organization=self.organization)

    def perform_create(self, serializer):
        serializer.save(organization=self.organization)
```

Every such endpoint needs a test proving that a member of another organization
gets a 404. `apps.organizations.testing.TenantTestCase` provides two
organizations with an owner, manager, and staff member;
`apps/inventory/tests.py` shows the pattern.

## Inventory

Each organization keeps a list of **ingredients**. Every ingredient has one
unit (`g`, `kg`, `ml`, `l`, or `each`) used for all of its quantities; there
are no unit conversions. Quantities are exact decimals with up to three
decimal places (at most 999,999,999.999) and are returned as strings.

Stock changes only by recording a **stock movement**:

- **Receive** adds a delivery to stock.
- **Count** sets stock to a counted quantity and records the difference. If the
  request includes `expected_quantity` (the level the counter started from)
  and stock has changed since, the count is refused with `409 Conflict` so a
  delivery recorded meanwhile is not undone. The frontend always sends it.

Movements are the ingredient's stock history: they cannot be edited or deleted
through the API or the Django admin. Stock never goes below zero.

| Action                                       | Owner | Manager | Staff |
| -------------------------------------------- | ----- | ------- | ----- |
| See ingredients, stock, and history          | yes   | yes     | yes   |
| Record deliveries and counts                 | yes   | yes     | yes   |
| Add, edit, archive, and delete ingredients   | yes   | yes     | no    |

An ingredient's unit can change, and the ingredient can be deleted, only until
it has stock history; after that, archive it instead. Archived ingredients are
hidden from the main list and cannot change stock.

**Retries are safe.** Every receive and count carries a `request_id` (a UUID
the client chooses once per submission). Sending the same request again
returns the movement already recorded (`200`) instead of changing stock twice;
reusing a `request_id` for a different change returns `409`. The frontend
keeps the same ID when you submit a form again after an error or a lost
connection, and disables the button while a submission is in flight.

Deliveries, counts, unit changes, and deletions of one ingredient lock its row,
so simultaneous requests are applied one at a time.

### Batches and expiry dates

Each organization has a **time zone** (an IANA name such as `Europe/London`,
default `UTC`). A new organization takes the creator's browser time zone, and
owners can change it on the dashboard. "Today", for everything about expiry,
is the date in that time zone.

Stock is held in **batches**:

- Every delivery creates its own batch. `expires_on` is optional (salt and dry
  goods have none); when given, it may not be in the past or more than 5 years
  ahead. Food can be used through its expiry date.
- A count that finds less stock than recorded takes the difference from
  batches **earliest expiry first**: expired stock first, then soonest-expiring,
  then batches without an expiry date, oldest delivery first.
- A count that finds more stock than recorded adds a batch with no expiry date.
- Stock that existed before batch tracking was moved into one opening batch
  per ingredient with no expiry date. Earlier stock history is unchanged.

Each stock change records **lines** saying how much it added to or took from
each batch, so every batch can be traced. An ingredient's stock always equals
the sum of its batches; `apps.inventory.stock.stock_problems()` checks this and
the other consistency rules.

Managers and owners can **correct a batch's expiry date** (for example, a
misread label). The correction is recorded in the stock history with the old
and new dates; past dates are allowed here.

A batch's status is `expired` (after its expiry date), `expiring` (within 3
days), `ok`, or `no_expiry`. The inventory screen lists everything expired or
expiring within 3 days, and each ingredient shows its next expiry date and how
much of it has expired.

| Endpoint (under `/api/organizations/<id>/`)   | Methods            |
| --------------------------------------------- | ------------------ |
| `ingredients/` (`?search=`, `?archived=true`) | GET, POST          |
| `ingredients/<ingredient id>/`                | GET, PATCH, DELETE |
| `ingredients/<ingredient id>/receive/`        | POST (`expires_on` optional) |
| `ingredients/<ingredient id>/count/`          | POST               |
| `ingredients/<ingredient id>/movements/`      | GET                |
| `ingredients/<ingredient id>/batches/` (`?include_empty=true`) | GET |
| `ingredients/<ingredient id>/batches/<batch id>/` | PATCH (managers and owners) |
| `expiring/` (`?within=` 0–30 days, default 3) | GET                |

### Not built yet

- Email verification, password reset, and email invitations (they need an email
  provider). Until then anyone can register an unverified address, including
  one that has a pending invitation. An admin can reset passwords in the Django
  admin.
- Member management and invitation screens in the frontend; staying signed in
  across reloads; frontend unit tests.
- Inventory: sales imports, waste logging, a stock history screen (the API has
  it), counting batch by batch, expiry notifications, unit conversions and
  purchase units (cases, packs), costs, suppliers, and multiple locations per
  organization.
- Multi-factor authentication, account lockout, deleting organizations, a
  Content Security Policy for the static site, and database row-level security.
- Rate-limit counters are kept per server process, and clients are identified
  by IP only once `DRF_NUM_PROXIES` is set for the hosting setup.
- Revoked and expired tokens accumulate; clear them periodically with
  `python manage.py flushexpiredtokens`.

## Configuration

Backend variables are documented in [backend/.env.example](backend/.env.example)
and frontend variables in [frontend/.env.example](frontend/.env.example).
Docker Compose already sets development values. Never commit a real `.env` file.

Security defaults:

- `DJANGO_DEBUG` is off unless explicitly enabled.
- The API refuses to start without `DJANGO_SECRET_KEY` and `DATABASE_URL`.
- CORS allows only the origins listed in `CORS_ALLOWED_ORIGINS`, without credentials.
- API endpoints require a signed-in user by default; only the health check,
  registration, and the sign-in token endpoints are public.
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
4. Open the frontend URL and confirm both statuses show `ok`, then create an
   account.

Free plan limits: the free Postgres database expires 30 days after creation
(deleted after a further 14-day grace period), and the free API service sleeps
after 15 minutes without traffic (the first request then takes about a minute).
Upgrade both before storing real data.
