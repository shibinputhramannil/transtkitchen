# Transt KitchenMind AI

Kitchen operations for independent restaurants, bakeries, and cloud kitchens:
sales-based demand forecasting, an editable prep planner, inventory and expiry
tracking, and waste logging.

> **Status: Phase 7 (production readiness).** People can register,
> sign in, create an organization, invite and manage their team, manage
> ingredients and stock with expiry dates, build menu items with recipes,
> import sales from CSV files, log waste, and plan prep from sales forecasts.
> The app is prepared for its first Render deploy (see
> [Deploy to Render](#deploy-to-render)) but not yet deployed.
> The interface uses the Transt green and cream brand theme.

## Stack

| Layer    | Technology                                      |
| -------- | ----------------------------------------------- |
| Frontend | React 19 + Vite, plain JavaScript, oxlint       |
| API      | Django 5.2 LTS + Django REST Framework          |
| Database | PostgreSQL 17                                   |
| Local    | Docker Compose                                  |
| Hosting  | Render: static site + Docker web service + Postgres |

```
backend/    Django project (config/) and apps (accounts, core, organizations, inventory, menu, sales, waste, planning)
frontend/   Vite React app
docker-compose.yml   local development stack
render.yaml          Render Blueprint for production
```

The interface uses the Transt brand theme: colour tokens in
[frontend/src/index.css](frontend/src/index.css) and the Transt logo in
`frontend/src/assets/brand/`. The Plus Jakarta Sans font is served from the
site itself (no external font requests, which keeps the Content Security Policy
strict) under the SIL Open Font License in `frontend/src/assets/fonts/OFL.txt`.

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
  apps/planning/        sales forecasts (forecasting.py) and prep plans
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
invitations expire after 7 days. Only the signed-in user whose email matches
an invitation can see, accept, or decline it. There is no email sending yet,
so tell people about their invitation yourself.

**Screens:** the **Team** page (from the dashboard) lists members and lets
owners change roles, owners and managers invite people and revoke pending
invitations (managers only for staff), and members remove others or leave as
their role allows. The dashboard, and the welcome screen for people without an
organization, show invitations addressed to you with Accept and Decline.
Owners rename the organization on the dashboard. The **Account** page (from
the header) edits your name and changes your password. The server checks every
permission; the screens only hide what your role cannot do.

**Changing your password** (`POST /api/auth/password/` with `current_password`
and `new_password`) checks the current password and the same password rules
as registration, signs you out everywhere else by revoking every earlier
token, and returns a fresh token pair for the current tab. It only ever
changes the signed-in user's own password and shares the sign-in rate limit.

### API

| Endpoint                                              | Methods          |
| ----------------------------------------------------- | ---------------- |
| `/api/auth/register/`                                 | POST             |
| `/api/auth/token/` (sign in)                          | POST             |
| `/api/auth/token/refresh/`                            | POST             |
| `/api/auth/logout/`                                   | POST             |
| `/api/auth/me/`                                       | GET, PATCH       |
| `/api/auth/password/`                                 | POST             |
| `/api/organizations/`                                 | GET, POST        |
| `/api/organizations/<id>/`                            | GET, PATCH       |
| `/api/organizations/<id>/members/`                    | GET              |
| `/api/organizations/<id>/members/<membership id>/`    | GET, PATCH, DELETE |
| `/api/organizations/<id>/invitations/`                | GET, POST        |
| `/api/organizations/<id>/invitations/<invitation id>/`| DELETE           |
| `/api/invitations/` (your pending invitations)        | GET              |
| `/api/invitations/<id>/accept/`, `.../decline/`       | POST             |
| `/api/csp-reports/` (browser CSP violation reports; no sign-in) | POST   |

Registration and sign-in are limited to 10 requests per minute per client, and
CSP violation reports to 60 (see [Security headers](#security-headers)).

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
through the API or the Django admin. Stock never goes below zero. The
**History** button on each ingredient lists every change, newest first: its
kind (delivery, count, sales, waste, waste voided, expiry correction), who made
it and when, the stock level after it, and the batches it touched.

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

## Forecasts and prep plans

**Forecasts** estimate the portions of each active menu item that will sell
on a business date, from applied sales imports only:

- A **trading day** is a date with any applied sales. On a trading day, an item
  with no sales sold 0. A date with no imported sales at all is unknown, not
  zero, so a missed import does not drag forecasts down. An item's history
  starts on its first appearance (its first sale or the day it was created,
  whichever is earlier).
- The forecast is the average of the last 4 trading days on the same weekday
  (within 8 weeks), if there are at least 3; otherwise the average of the last
  14 trading days, if there are at least 7; otherwise there is not enough
  history and the manager decides. The API lists the days and quantities
  every forecast used; the Prep screen shows the quantities for weekday
  averages and the number of days for 14-day averages.

**Prep plans** say how many portions of each menu item to prepare for one
business date, from today up to 14 days ahead; there is one plan per date.

- A new plan takes the forecast plus a buffer (10% by default, editable per
  plan), rounded up to whole portions. Managers and owners change planned
  portions, notes, and the buffer; staff can view plans.
- **Refresh forecast** recalculates forecasts and updates lines nobody has
  edited; edited lines keep their planned portions. Viewing a plan never
  changes it.
- Every change sends the plan's `version`; if someone else changed the plan
  first, the change is refused (409) instead of overwriting theirs.
- **Ingredient needs** come from the planned portions through the recipes, in
  each ingredient's stock unit. They are shown against **current stock (live)**,
  with the time they were calculated: stock that will have expired before the
  plan date is not counted as usable, the shortfall is what is still needed,
  and batches close to expiry on the plan date are listed to use first. Menu
  items without a recipe are flagged.
- **Finalizing** locks the plan and saves a **snapshot** of its ingredient needs
  and the stock figures of that moment, labelled with when it was taken. The
  snapshot is history, not current stock; the live figures are always
  calculated separately. Managers can reopen a plan until its date has passed,
  which discards the snapshot.
- Each line also shows the item's recent overproduction waste. Once a plan's
  date has passed, it shows what actually sold (when that day's sales are
  imported) and what was overproduced.

Plans never change stock: deliveries, counts, sales, and waste do.

| Endpoint (under `/api/organizations/<id>/`)   | Methods                       |
| --------------------------------------------- | ----------------------------- |
| `forecast/` (`?date=`, default tomorrow)      | GET                           |
| `prep-plans/` (`?from=`, `?to=`)              | GET, POST (`{plan_date, buffer_percent?}`) |
| `prep-plans/<id>/`                            | GET, PATCH (`{version, buffer_percent?, lines: [{menu_item, planned?, note?}]}`), DELETE (drafts) |
| `prep-plans/<id>/refresh/`, `finalize/`, `reopen/` (`{version}`) | POST       |

### Not built yet

- Email verification, password reset, and email invitations (they need an email
  provider). Until then anyone can register an unverified address, including
  one that has a pending invitation. An admin can reset passwords in the Django
  admin.
- Staying signed in across reloads; frontend unit tests.
- Inventory: counting batch by batch, expiry notifications, purchase units
  (cases, packs), costs, suppliers, and multiple locations per organization.
- Waste: money values (there are no ingredient costs yet), photos, targets and
  alerts, editable reasons, and supplier returns.
- Planning: holiday and event adjustments, trend models, accuracy dashboards,
  automatic waste-based adjustment, sub-recipes and shared components (a sauce
  used by several dishes), prep by station or time, purchase orders, printing
  and exports, and notifications.
- Sales: point-of-sale integrations, choosing CSV columns, refunds, undoing an
  applied import, a business day that ends after midnight, prices and revenue,
  recipe sub-components and modifiers, and charts.
- Multi-factor authentication, account lockout, deleting organizations, and
  database row-level security. The static site's Content Security Policy ships
  in report-only mode until it is checked on the deployed site.
- Rate-limit counters are kept per server process, and clients are identified
  by IP only once `DRF_NUM_PROXIES` is set for the hosting setup.
- Expired tokens are deleted at every API start; a long-running server
  without restarts would need `python manage.py flushexpiredtokens` run
  periodically.

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

[render.yaml](render.yaml) is a Render Blueprint that creates three resources:

| Resource          | Type                | Plan | Region    |
| ----------------- | ------------------- | ---- | --------- |
| `kitchenmind-db`  | PostgreSQL 17       | free | Singapore |
| `kitchenmind-api` | Docker web service  | free | Singapore |
| `kitchenmind-web` | Static site         | free | global CDN (static sites take no region) |

The API and database share a region so the API can use the database's private
connection string. **Render does not allow changing a region or the Postgres
major version after creation**, so confirm both before applying the Blueprint.

### Before you start

- The repository must be on GitHub, GitLab, or Bitbucket, and connected to your
  Render account.
- Decide on the region. Singapore is closest to kitchens on India time; Render
  lists it as available, but its documentation does not say whether every free
  plan is offered there. If Render rejects the Blueprint over the region or
  plan, pick another region in `render.yaml` (for both the API and the
  database) rather than letting them differ.
- Decide whether you want automatic deploys. By default Render redeploys a
  service whenever its files change on the linked branch; you can switch this
  off in each service's settings.
- Know the free-plan limits:
  - The free database **expires 30 days after creation** and is deleted after a
    further 14-day grace period. It has no backups. **Upgrade it to a paid plan
    before storing real data.**
  - The free API **sleeps after 15 minutes without traffic**. The next request
    waits for it to start again (often around a minute). Each start also runs
    migrations, token cleanup, and the admin bootstrap (see below).
  - Free web services share 750 instance hours per workspace per month.
  - The free plan has no shell, one-off jobs, or pre-deploy command, which is
    why the API's startup script does that work.

### Node version

The static site's build uses the Node.js major version in
[.node-version](.node-version) (`24`). A `NODE_VERSION` environment variable on
the service would override it, so leave that unset.

### Environment variables

| Service           | Variable                       | Value |
| ----------------- | ------------------------------ | ----- |
| `kitchenmind-api` | `DATABASE_URL`                 | Set by the Blueprint (the database's private URL). |
|                   | `DJANGO_SECRET_KEY`            | Generated by the Blueprint. Changing it signs everyone out. |
|                   | `DJANGO_DEBUG`                 | `false` (set by the Blueprint). Never enable it. |
|                   | `CORS_ALLOWED_ORIGINS`         | Entered when applying: the frontend URL, e.g. `https://kitchenmind-web.onrender.com` (no trailing slash). |
|                   | `CSRF_TRUSTED_ORIGINS`         | Entered when applying: the same frontend URL. |
|                   | `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD` | Temporary, for the first admin only. See [First admin account](#first-admin-account). |
|                   | `DRF_NUM_PROXIES`              | Unset at first. See [Rate limiting behind Render's proxy](#rate-limiting-behind-renders-proxy). |
|                   | `DJANGO_SECURE_HSTS_SECONDS`   | Optional, default `3600`. Raise it last. See [HTTPS and HSTS](#https-and-hsts). |
| `kitchenmind-web` | `VITE_API_URL`                 | Entered when applying: the API URL, e.g. `https://kitchenmind-api.onrender.com`. Built into the bundle, so redeploy the static site after changing it. |

Render adds the API's own `onrender.com` hostname to the allowed hosts
automatically. The other options are described in
[backend/.env.example](backend/.env.example).

### Stage 1: create the services

1. In Render, choose **New → Blueprint** and select the repository and branch.
2. Review the plan Render shows: three resources, the regions above, and
   Postgres 17. Stop if anything differs.
3. Enter the three values marked as entered when applying (above), then apply.

Render may add a suffix to a name that is already taken, which changes that
service's URL. If it does, update `CORS_ALLOWED_ORIGINS`,
`CSRF_TRUSTED_ORIGINS`, and `VITE_API_URL` to the URLs shown in the dashboard,
and change the API origin in the CSP's `connect-src` in `render.yaml` (see
[Security headers](#security-headers)).

**Verify:**

- The API's deploy succeeds and its log shows the migrations, then gunicorn
  listening. If the deploy fails its health check while the API URL works in a
  browser, look for `400` responses to `/api/health/` in the API log: that
  would mean Render's health check sends a host name that is not in the allowed
  hosts. This has not been checked against a real deploy.
- `https://<api>/api/health/` returns `{"status": "ok", "database": "ok"}`.
- The frontend loads, shows both statuses as `ok`, and a deep link such as
  `https://<web>/inventory` loads the app rather than a 404 (the Blueprint
  rewrites every path to `index.html`).
- You can create an account and an organization, and the browser console shows
  no CORS errors.

**Roll back:** Render can redeploy any previous successful deploy of a service
from its **Events** page. A rollback does not undo database migrations, so a
migration that needs reversing needs its own fix.

### First admin account

The free plan has no shell, so `createsuperuser` cannot be run there. Instead,
the API's startup script runs `bootstrap_admin`, which creates a Django admin
account from two temporary variables:

1. On `kitchenmind-api`, add `DJANGO_SUPERUSER_EMAIL` and
   `DJANGO_SUPERUSER_PASSWORD` (a long, unique password; it must meet the same
   rules as registration). Saving them redeploys the API.
2. The API log shows `Admin bootstrap: created the admin account <email>`.
   The password is never logged.
3. Sign in at `https://<api>/admin/`.
4. **Remove both variables** (this redeploys the API again), then change the
   password from the admin's user page if you want one that never sat in the
   dashboard.

The command is safe to repeat: without the variables it does nothing, and if
an account with that email already exists it changes nothing about it (not
even the password) and only logs a reminder to remove the variables. Setting
only one of the two, an invalid email address, or a password that fails the
rules stops the API from starting, with the reason in the log.

The Django admin sign-in has no rate limit of its own, so keep admin
passwords strong and remove admin rights (`is_staff`) from accounts that no
longer need them.

### Token cleanup

Every API start runs `flushexpiredtokens`, which deletes sign-in tokens that
have expired. Because the free API restarts after every sleep, this keeps the
token tables small without a scheduled job. (Render cron jobs are not free.)

### Security headers

The static site sends `X-Frame-Options: DENY`, `X-Content-Type-Options:
nosniff`, `Referrer-Policy: same-origin`, a `Permissions-Policy` that turns
off camera, microphone, location, and payments, and a Content Security Policy.

The CSP starts in **report-only** mode (`Content-Security-Policy-Report-Only`):
the browser reports violations in the console but blocks nothing. It allows
only the site's own scripts, styles, fonts, and images (plus `data:` images),
and network requests to the site itself and the API. It was tested enforced
against a local production build on every screen with no violations, but
not yet against the deployed site. To enforce it:

1. Confirm the API origin in `connect-src` matches the real API URL exactly.
2. Open the deployed site and go through every screen (dashboard, Inventory
   including stock history, Menu, Sales including the template download and an
   import, Waste, Prep, Team, Account) with the browser console open. Look for
   `Content-Security-Policy` report messages.
3. If there are none, rename the header in `render.yaml` to
   `Content-Security-Policy` and redeploy. Check the console again.

Header changes in `render.yaml` apply when the Blueprint syncs. If you edit
headers in the dashboard instead, mirror the change in `render.yaml`.

**Violation reports.** The API accepts browsers' CSP violation reports at
`POST /api/csp-reports/`, in both the `report-uri` and the Reporting API
(`report-to`) formats: no sign-in, at most 16 KB, and 60 requests a minute per
client. It replies `204`, or `413` for a body that is too large and `429` when
the rate limit is reached. It stores nothing. Each distinct violation is written to the API log at most once
an hour per server process, as one line: the disposition, the directive, the
blocked resource reduced to a keyword such as `inline` or to its origin, the
page path with IDs replaced by `:id` and without its query string, the source
file on the frontend's own origin with line and column, the HTTP status, and
the browser family and major version. This line never contains IP addresses,
full user agents, referrers, code samples, the policy text, or query strings,
and reports for other sites or caused by browser extensions are ignored. Like
every API request, each report also appears in gunicorn's access log, which
records the connecting address and the full user agent. The
static site's policy does not send reports here yet: that needs
`report-uri` and `report-to` in its policy and a `Reporting-Endpoints` header in
`render.yaml`.

### Rate limiting behind Render's proxy

Registration and sign-in are limited to 10 requests a minute per client. Render
puts a proxy in front of the API, so the API sees the proxy's address unless
`DRF_NUM_PROXIES` tells it how many proxies append to `X-Forwarded-For`.
Render does not document that number. With the variable unset, every client
shares one limit; a value that is too large lets anyone choose their own
address by sending a fake `X-Forwarded-For` header, and so bypass the limit.

Find the value without logging any client addresses:

1. Set `DRF_NUM_PROXIES=1` and let the API redeploy.
2. From one network (for example home Wi-Fi), sign in with a wrong password
   until you get `429 Too Many Requests`.
3. Within the same minute, from a different network (for example a phone on
   mobile data), try once. A `429` here means clients are still sharing a
   limit: try `2` and repeat. A normal error (`401`) means clients are told
   apart.
4. From the first network, wait for the limit to clear, exceed it again while
   sending a made-up `X-Forwarded-For: 203.0.113.7` header with each request,
   and confirm you still get `429`. If the fake header lets you through, the
   value is too large.

If no value passes both checks, leave the variable unset and note that sign-in
limits are shared. The counters are kept per server process, so with more than
one gunicorn worker (`WEB_CONCURRENCY`) the effective limit is that many times
higher.

### HTTPS and HSTS

With `DJANGO_DEBUG=false`, the API redirects HTTP to HTTPS (except the health
check), marks cookies secure, and sends HSTS for one hour
(`DJANGO_SECURE_HSTS_SECONDS=3600`). Once everything above works, raise it, for
example to `31536000` (a year). Do this last: browsers remember HSTS, and it
cannot be withdrawn early. Subdomains and preloading stay off, because
`onrender.com` is shared.

### Protecting the data

- **Upgrade the database to a paid plan before storing real data.** Free
  databases expire and have no backups. Paid plans include point-in-time
  recovery (3 days on Hobby, 7 days on Pro at the time of writing).
- The Blueprint does not restrict external database access, so Render's
  default applies. In the database's **Networking** settings, check which
  addresses can connect from outside Render and narrow the list to your own
  address, or to none.
- To take a manual backup, run `pg_dump` with the database's **external**
  connection string from your own machine, and keep the file somewhere safe and
  private. Never commit it.
- Never reset, delete, or recreate the Render database without a fresh backup.

### Checks that need the real deploy

These cannot be verified locally and should be ticked off on the first deploy:

- [ ] Singapore and the free plans are accepted, with Postgres 17.
- [ ] Final service URLs, and therefore `CORS_ALLOWED_ORIGINS`,
      `CSRF_TRUSTED_ORIGINS`, `VITE_API_URL`, and the CSP's `connect-src`.
- [ ] The health check passes (the host name it sends is allowed).
- [ ] Deep links load through the rewrite, and the static site sends the
      security headers.
- [ ] Only the expected service redeploys when `backend/` or `frontend/` changes.
- [ ] No CSP reports on any screen, then the CSP is enforced.
- [ ] `DRF_NUM_PROXIES` is set by the procedure above, or left unset on purpose.
- [ ] `WEB_CONCURRENCY` and memory use on the free instance.
- [ ] The first admin exists and both bootstrap variables are removed.
- [ ] External database access is narrowed.
- [ ] The database is upgraded before real data, or its expiry date is noted.
- [ ] HSTS is raised once everything else works.
