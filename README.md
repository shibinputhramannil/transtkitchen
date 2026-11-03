# KitchenMind AI

Kitchen operations for independent restaurants, bakeries, and cloud kitchens:
sales-based demand forecasting, an editable prep planner, inventory and expiry
tracking, and waste logging.

> **Status: Phase 4d (waste logging).** People can register, sign in, create
> an organization, manage ingredients and stock with expiry dates, build menu
> items with recipes, import sales from CSV files, and log waste, all of which
> keep stock up to date batch by batch. Forecasting and prep planning come next.

## Stack

| Layer    | Technology                                      |
| -------- | ----------------------------------------------- |
| Frontend | React 19 + Vite, plain JavaScript, oxlint       |
| API      | Django 5.2 LTS + Django REST Framework          |
| Database | PostgreSQL 17                                   |
| Local    | Docker Compose                                  |
| Hosting  | Render: static site + Docker web service + Postgres |

```
backend/    Django project (config/) and apps (accounts, core, organizations, inventory, menu, sales, waste)
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
                        stock.py holds the rules every stock change follows,
                        units.py the unit conversions
  apps/menu/            menu items, recipes (recipes.py: portions to ingredient
                        quantities), and remembered sales item names
  apps/sales/           sales imports: CSV parsing, preview, applying, reports
  apps/waste/           waste entries, voiding, and waste reports
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

## Menu and recipes

Each organization has **menu items**, and each item has a **recipe**: how much
of each ingredient one portion uses. A recipe line may use a different unit
from the ingredient's stock unit, as long as both measure the same thing:
grams and kilograms, millilitres and litres, or counted items (`each`). Recipes
can only use the organization's own ingredients. An ingredient used in a recipe
cannot be deleted or switched to a different kind of unit (kg to l).

Staff can see the menu; managers and owners edit it. Items that appear in
imported sales can be archived but not deleted.

| Endpoint (under `/api/organizations/<id>/`)   | Methods                       |
| --------------------------------------------- | ----------------------------- |
| `menu-items/` (`?search=`, `?archived=true`)  | GET, POST                     |
| `menu-items/<id>/`                            | GET, PATCH, DELETE            |
| `menu-items/<id>/recipe/`                     | GET, PUT (replaces the recipe) |

## Sales imports

Managers and owners import sales from a CSV file; staff can view imports and
reports. The file needs a header row with `date`, `item`, and `quantity`
(matched ignoring case; `item_id` is optional, other columns are ignored):

```
date,item,quantity,item_id
2026-11-02,Margherita Pizza,14,
2026-11-02T19:30:00+05:30,Flat White,2,SKU-118
```

- `date` is either the kitchen-local business date (`YYYY-MM-DD`) or an ISO
  date-time. With a UTC offset it is converted to the kitchen's time zone;
  without one it is read as kitchen-local time. Other formats such as
  `02/11/2026` are rejected.
- `quantity` is a positive number with up to 3 decimal places. Refunds
  (negative quantities) are not supported.
- Files must be UTF-8 (Excel's "CSV UTF-8"), at most 2 MB and 5,000 rows.
  Files with duplicate or empty column headers are rejected. These checks run
  before anything is stored; the file itself is never stored, only its rows and
  a hash of its contents.

**1. Upload.** The upload creates a draft and shows a preview; nothing changes
yet. Rows with problems are listed with their row number. Uploading the same
file again returns the same draft (or 409 if it was already applied).

**2. Match items.** When the file is uploaded, each row's item is matched to a
menu item by a remembered item ID or name, or by the menu item's own name
(ignoring case). Unmatched names must be mapped to a menu item, which is
remembered for later imports, or ignored for this import.

Viewing the preview never changes the draft. If menu items or remembered names
change after the upload, the preview reports how many rows would now match
differently (`rematch_available`); **Re-match names** (`POST .../rematch/`)
updates them. Re-matching leaves ignored rows alone and is safe to repeat.
Applying uses the matches exactly as the preview shows them.

**3. Check warnings.** The preview lists what will be deducted per ingredient
and these warnings, each of which must be acknowledged before applying:

- `row_errors`: rows with errors will be left out.
- `shortfalls`: recorded stock is not enough. The sales are still recorded,
  stock is taken down to zero (never below), and the missing amount is
  recorded as a shortfall. Count those ingredients afterwards.
- `overlapping_dates`: another applied import already has sales on some of
  these dates; if they are the same sales, applying counts them twice.
- `counted_after_sales`: an ingredient was counted after a sale that used it,
  so the count may already reflect that sale and deducting it again could
  count it twice. A sale happened at its time if the file gave one, otherwise
  at the start of its business date in the kitchen's time zone.

Recipes that use archived ingredients block applying.

**4. Apply.** Applying records the sales and deducts each ingredient from its
batches, earliest expiry first, in one transaction: if anything fails,
nothing is deducted and the import stays a draft. Stock is deducted from the
batches that exist when the import is applied, so import sales daily. Applying
an import that is already applied returns the result again without deducting
twice, and two imports are applied one at a time. An applied import cannot be
undone; correct stock with a count. Drafts can be discarded.

The **sales report** gives the quantity sold per menu item per business date
(kitchen-local), from applied imports.

| Endpoint (under `/api/organizations/<id>/`)   | Methods                       |
| --------------------------------------------- | ----------------------------- |
| `sales-imports/`                              | GET, POST (form field `file`) |
| `sales-imports/template.csv`                  | GET                           |
| `sales-imports/<id>/` (preview)               | GET                           |
| `sales-imports/<id>/rows/` (`?status=error\|unmatched\|ignored\|matched`) | GET |
| `sales-imports/<id>/mappings/` (`{item_name, menu_item}` or `{item_name, ignore: true}`) | POST |
| `sales-imports/<id>/rematch/`                 | POST                          |
| `sales-imports/<id>/apply/` (`{acknowledged: [...]}`) | POST                  |
| `sales-imports/<id>/discard/`                 | POST                          |
| `sales/daily/` (`?from=`, `?to=`, up to 92 days; default the last 7) | GET    |

Imports are designed to accept point-of-sale integrations later: each import
records its `source` and a `source_reference` (for CSV files, the file's hash)
that may be used only once.

## Waste

Any member can log waste; managers and owners can void entries. A waste entry
is one of:

- **An ingredient**, in a unit compatible with its stock unit (300 g of flour
  stocked in kg). Stock is taken earliest expiry first, or from one chosen
  batch: the **Discard** button on a batch (and on expired stock in the
  "Expiring soon" list) logs its whole remaining quantity, with the reason
  "Expired" when it has expired.
- **A menu item**, in portions. Its recipe says which ingredients went to
  waste with it, exactly as for sales. Items without a recipe are recorded but
  deduct nothing.

Reasons are `expired`, `spoiled`, `damaged`, `overproduction`, `quality`, and
`other`. The waste date is the kitchen-local business date: today by default,
up to 7 days back, never in the future.

Logging is one transaction: the entry, its stock changes, and its batch lines
are saved together or not at all. Each submission carries a `request_id`, so
sending it again returns the entry already logged instead of logging twice.
Two situations must be acknowledged first, as with sales imports:

- `shortfall`: recorded stock is less than the waste. Stock is taken down to
  zero, never below, and the rest is recorded as a shortfall.
- `counted_after`: an ingredient was counted after the waste may have happened,
  so the count may already leave it out and deducting it again could count it
  twice. Waste for today happened when it was logged; waste for an earlier day
  is treated as happening at the start of that day in the kitchen's time zone.

Entries are never edited. **Voiding** an entry (with a reason) puts back
exactly the stock it took, into the same batches it came from, and leaves the
entry out of reports. Voiding again changes nothing. Archived ingredients
cannot change stock, so restore them before logging or voiding their waste.

The **waste summary** covers a date range (default the last 7 days) and never
adds quantities across units: per ingredient and unit, per reason (entry
counts, quantities per unit, and portions), and per menu item (portions).

| Endpoint (under `/api/organizations/<id>/`)   | Methods                       |
| --------------------------------------------- | ----------------------------- |
| `waste/` (`?from=`, `?to=`, `?reason=`, `?ingredient=`, `?include_voided=true`) | GET, POST |
| `waste/<id>/`                                 | GET                           |
| `waste/<id>/void/` (`{reason}`)               | POST (managers and owners)    |
| `waste/summary/` (`?from=`, `?to=`, up to 92 days) | GET                      |

### Not built yet

- Email verification, password reset, and email invitations (they need an email
  provider). Until then anyone can register an unverified address, including
  one that has a pending invitation. An admin can reset passwords in the Django
  admin.
- Member management and invitation screens in the frontend; staying signed in
  across reloads; frontend unit tests.
- Inventory: a stock history screen (the API has it), counting batch by batch,
  expiry notifications, purchase units (cases, packs), costs, suppliers, and
  multiple locations per organization.
- Waste: money values (there are no ingredient costs yet), photos, targets and
  alerts, editable reasons, and supplier returns.
- Sales: point-of-sale integrations, choosing CSV columns, refunds, undoing an
  applied import, a business day that ends after midnight, prices and revenue,
  recipe sub-components and modifiers, and charts.
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
