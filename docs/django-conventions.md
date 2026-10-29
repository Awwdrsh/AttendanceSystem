# Django conventions (idiomatic)

The rulebook every slice follows, so the app reads as one idiomatic Django product. Follow it literally. The
foundation it describes lives in `hajiri.core` (auth, companies, scoping, validation, DRF integration, settings store,
collation, images, mail), `hajiri.db.models` (models, choices, company querysets, SQL-function expressions),
`hajiri.worker` (django.tasks) and `hajiri.web` (middleware, serving the React app). The web app itself is the React
app in `frontend/` (plain JavaScript); the backend's job is the JSON API it calls, with the TS API's paths, bodies,
field names, status codes and error shapes. The help desk (`hajiri/helpdesk/`) is the worked example: every pattern
below appears there, and section 15 shows it before and after the conversion.

If you need something that is not here, list it in `sharedChangesNeeded`; do not write your own framework.

## 1. Files in a slice

```
server/hajiri/<app>/
  services.py     business rules: ORM queries, transactions, audit, notifications; no request objects;
                  raises hajiri.core.errors; validates TS-shaped bodies with its serializers
  serializers.py  DRF serializers: the request contract (the TS zod schemas) and JSON response shapes
  api.py          DRF views/viewsets: the JSON endpoints the React app (and Bearer clients) call
  jobs.py         background jobs registered with hajiri.worker.registry (daily/periodic)
  urls.py         urlpatterns of api.py (mounted at the site root with namespace = app name); DRF routers
server/hajiri/<app>/templates/<app>/
  print/*.html    only for HTML the API itself returns (certificates, offer letters, review print-outs)
server/tests/<app>/
  conftest.py     from tests.web.fixtures import *   (optional)
  test_services.py, test_api_*.py
frontend/src/
  pages/<area>/*.jsx, api/<module>.js   the screens and their API calls (plain JavaScript)
```

## 2. Models and the ORM

All tables are models in `hajiri.db.models` (one module per domain, one Django app labelled `hajiri`). Import from
the package: `from hajiri.db.models import HelpdeskTicket, Employee`.

**Choices.** Every CHECK-constrained text column has a nested `TextChoices` named after the column. Use the members,
never string literals, in queries and comparisons:

```python
HelpdeskTicket.Status.IN_PROGRESS        # 'in_progress', label 'In progress'
AttendanceDay.Status.HALF_DAY_LEAVE      # 'HalfDayLeave'
AppUser.Role.PAYROLL_ADMIN               # roles array items
qs.filter(status__in=(Status.OPEN, Status.IN_PROGRESS, Status.WAITING))
```

**Related names** say what the relation is: the main link is the plural table name (`company.helpdesk_tickets`,
`employee.attendance_days`); further links to the same model add `_as_<role>`
(`app_user.helpdesk_tickets_as_assignee`, `employee.branches_as_head`); self-references to a parent are
`children`; one-to-one links are the singular table name (`employee.app_user`). Every model has a `__str__`.

**Query patterns** (the ORM first; section 12 says when raw SQL is allowed):

| need | write |
|---|---|
| joins for display | `select_related('category', 'assignee_user')`, or `annotate(category_name=F('category__name'))` + `.values(...)` |
| many-side loads | `prefetch_related('helpdesk_messages')` |
| counts and sums | `aggregate(open=Count('id', filter=Q(status__in=ACTIVE)))`, `values('company_id').annotate(n=Count('id'))` |
| "the newest version" | `exclude(Exists(Model.objects.filter(key=OuterRef('key'), version__gt=OuterRef('version'))))` |
| a value from a related row | `Subquery(qs.filter(company=OuterRef('pk')).values('status')[:1])`, `ArraySubquery(...)` |
| a company setting next to a row | `hajiri.core.settings_store.setting_subquery('notifications.policy', 'slaHours', output_field=IntegerField())` |
| conditional values | `Case(When(status=Status.OPEN, then=Value(Status.IN_PROGRESS)), default=F('status'), output_field=TextField())` |
| ordering by a condition | `order_by(ExpressionWrapper(Q(status__in=DONE), output_field=BooleanField()), '-updated_at')` |
| atomic counters | `.filter(pk=..., attempts__lt=5).update(attempts=F('attempts') + 1)` and check the returned row count |
| upsert | `bulk_create(rows, update_conflicts=True, unique_fields=[...], update_fields=[...])`; `get_or_create` when you need "did I create it" |
| locks | `select_for_update()` (`skip_locked=True` for queues, `no_key=True` to serialise on a parent row without blocking inserts that reference it, `of=('self',)` with outer joins) |
| several writes | `with transaction.atomic():` (except steps that count failures, which must survive the error) |
| JSON keys | `filter(value__date=today)`, `KT('payload__ticketId')` for `payload->>'ticketId'` |

`update()` and `F()` see the row as it was before the UPDATE, exactly like a SQL `CASE` in `SET`. Fields with a
`db_default` can be left out of `create()`; non-null text fields without one must be set (the CHECKs refuse `''`).
Timestamps from `Now()` in one transaction are equal: break ties with the uuidv7 `id` (`order_by('created_at', 'id')`).

**Database functions as expressions** (`hajiri.db.models.Manages`, `IsHead`, or `hajiri.core.auth.manages`):

```python
from hajiri.core.auth import manages
Employee.objects.for_company(cid).filter(manages(user.employee_id))                       # target: the row id
AttendanceDay.objects.filter(manages(user.employee_id, F('employee_id'), F('local_date'))) # on that day
Employee.objects.annotate(head=IsHead(F('id')))
```

Business dates are Nepal dates: pass `hajiri.core.clock.npt_today()`, never `date.today()` or a UTC date.

## 3. Company scoping and groups of companies

A person can work in several companies of a group (`company_group`, `company.group_id`). Access comes from three
places, merged by `hajiri.core.auth.company_access`:

| source | table | roles there |
|---|---|---|
| home company | `app_user.company_id` | `app_user.roles`, employee `app_user.employee_id` |
| another company | `company_membership` | its own `roles` and `employee_id` |
| every company of a group | `group_member` | `group_admin` gives admin, `group_hr` gives hr, `group_executive` gives executive (no employee record) |

Each session works in one **active** company (`session.active_company_id`, switched with
`hajiri.core.auth.switch_company`). `request.hajiri_user` (and `request.user` in DRF views) always describes the
ACTIVE company:

| `AuthUser` | meaning |
|---|---|
| `company_id` | the active company: scope everything by it |
| `roles`, `employee_id` | the person's roles and employee record in the active company (group roles already merged in) |
| `companies` | every company they may switch to, as `CompanyRef(id, code, name, roles, employee_id, home, group_id, via_group, is_default)` |
| `company_ids`, `multi_company`, `home_company_id` | the same ids (home first); more than one; the account's home company |
| `group_id`, `group_roles`, `has_group_role()` | the active company's group and the person's group roles in it |

So `has_role(user, 'hr')` is already right for a group HR person working in a sister company: **never test
`group_roles` to grant company work**. Use `group_roles` only for group-level actions (adding companies, group
roles) and for showing the Group hub.

**Every query that reads or writes company data is scoped to the active company**:

```python
from hajiri.core.auth import company_id_of
from hajiri.core.scoping import get_or_404

cid = company_id_of(request.hajiri_user)                        # ForbiddenError if the account has no company
HelpdeskTicket.objects.for_company(cid).filter(status=HelpdeskTicket.Status.OPEN)
AttendanceDay.objects.for_company(cid)                          # scoped through employee (company_scope)
ticket = get_or_404(HelpdeskTicket, cid, 'ticket not found', pk=ticket_id)
```

- Each company-data model declares `company_scope` (a lookup path to its company: `'company'`,
  `'employee__company'`, ...) and uses `CompanyQuerySet`; `for_company(None)` is empty.
- An id of another company behaves as **not found** (404, never 403), so ids cannot be probed across tenants. This
  holds even for a person who may switch to that company: until they switch, it is not their company.
- Never trust ids from a form, query string or body: re-read them through `for_company` (including ids inside
  lists, such as approvers, members or employee pickers).
- A `GET` that only renders a form for a record (an edit modal, an "add input to this run" form) still loads the
  record first, so a foreign id is 404 before anything is drawn.
- People of a company are its home accounts plus members plus group-role holders: `AppUser.objects.for_company(cid)`,
  `hajiri.core.users.company_users(cid)`, `company_roles(cid, user_id)` (roles there, None if they cannot act
  there) and `user_ids_with_role(cid, role)`. Do not filter `app_user.company_id` (that is only the home company)
  for "who works here", "who gets this notification" or "who can approve".
- An employee record belongs to exactly one company. A person who works in two companies has two employee records
  (one per company); use `user.employee_id`, which follows the active company, never `app_user.employee_id`.
- Background jobs have no active company: they loop over companies (`Company.objects.all()` or the rows they
  process) and scope each step with that row's company id.
- Manager scoping: `assert_manages(user, employee_id, area=None)` for one employee, `manages(...)` in list queries.

### Group-wide views

Group views (`hajiri.group`, `hajiri.group_admin`, `hajiri.web.group`) are the **only** code that reads across
companies, and they follow these rules:

- Read only over the companies the person may see: `hajiri.core.companies.group_company_ids(user, *roles)` (default:
  companies where they are admin, HR or executive, directly or through a group role), with
  `Model.objects.for_companies(ids)`. Never `Model.objects.all()`, never the whole group.
- Show figures per company and totals; link to a record only through a switch to its company (POST
  `/api/v1/me/companies/switch`), never by opening a foreign id in the active company.
- Writes go to one company at a time and only through the active company (switch first), except group
  administration in `hajiri.core.companies` (`create_group_company`, `grant_access`, `grant_group_role`, ...), which
  checks `is_group_admin` / `has_role(user, 'admin')` itself and audits every change.
- Who may use the group endpoints: `hajiri.core.companies.can_open_group(user)`; the overview:
  `can_view_group(user)`; group administration: `is_group_admin(user)`.

### Testing isolation

`tests/multicompany/conftest.py` builds a group of two companies plus one outside it, with people who reach one
company, two companies, or every company through a group role (`world` fixture). A new JSON endpoint taking an id
gets a case in `tests/multicompany/test_isolation.py` (or the same three requests in the
slice's tests): the outsider and the group admin working in the other company get 404, the owner company's admin
does not.

## 4. Validation: serializers for the contract

The request contract of a service (the TS zod schema) is a **DRF serializer** with the TS body keys as field names.
`hajiri.core.validation.validate(Serializer, data)` returns the validated dict or raises
`hajiri.core.errors.ValidationError` with `[{path, message}]` in the zod wording ("Required", "Too big: expected
string to have <=160 characters", "Invalid option: expected one of ...", "Invalid UUID"). Messages you write yourself
are kept.

```python
class TicketIn(serializers.Serializer):
    categoryId = serializers.UUIDField()
    subject = serializers.CharField(min_length=3, max_length=160)        # trims, like zod .trim()
    priority = serializers.ChoiceField(HelpdeskTicket.Priority.values, default=HelpdeskTicket.Priority.NORMAL)
    fileIds = serializers.ListField(child=serializers.UUIDField(), max_length=5, default=list)

b = plain(validate(TicketIn, body))           # plain(): UUIDs back to strings
```

- `CharField` trims by default: pass `trim_whitespace=False` where zod had no `.trim()`; a string without a minimum
  needs `allow_blank=True`; zod `nullish` is `required=False, allow_null=True`.
- TS string formats: `IsoDateField` ('YYYY-MM-DD', real dates), `HhmmField`, `MoneyField` (amount as a string);
  `parse_uuid(value)` for a path parameter.

The React app shows the `issues` itself (`frontend/src/api/client.js` normalizeError joins them as
`path: message`), so the wording is the TS wording.

## 5. JSON endpoints: Django REST framework

Every endpoint the React app calls, and those of terminals, kiosk tablets and Bearer-token integrations, under
`/api/v1/...` with the TS path. They are DRF views or viewsets; the defaults in `REST_FRAMEWORK` (config/settings.py,
`hajiri.core.api`) give:

- **Authentication** `SameOriginAuthentication`: the `hajiri_session` cookie, accepted for a write only when its
  `Origin` (else `Referer`) is this site or a `CORS_ORIGINS` entry (the TS `sameOriginGuard`; the React app sends no
  CSRF token), or `Authorization: Bearer <token>`. `hajiri.web.middleware.SameOriginMiddleware` applies the same
  check to every cookie request, so Django's CSRF middleware is not installed. `request.user` is the `AuthUser`
  (active company); `request.auth` the session token.
- **Permissions** (default `IsSignedIn`): `IsSignedIn` (password changed, two-step set up when required),
  `SignedInAnyState`, `HasRole('hr', 'manager')`, `HasModule('payroll')`, `FeatureEnabled('expenses')`. Refusals use
  the TS messages (`requires role hr`, `password change required`, `feature off`).
- **Errors** in the TS shape (`exception_handler`): `{"error": message}` (+ details), validation
  `{"error": "validation", "issues": [...]}`, database errors like the TS handler, wrong method 404.
- **Rendering**: JSON only; timestamps ISO UTC with milliseconds (`...T04:19:00.123Z`), decimals and UUIDs as strings.
- **Pagination** `?page=&pageSize=` answered as `{"items", "total", "page", "pageSize"}`; filtering with
  django-filter `FilterSet`s (`DEFAULT_FILTER_BACKENDS`) on querysets.
- **Schema**: drf-spectacular at `/api/schema`, Swagger UI at `/api/docs` (admins). Annotate with `@extend_schema`.

```python
class HelpdeskViewSet(viewsets.ViewSet):
    permission_classes = [IsSignedIn, FeatureEnabled('helpdesk')]

    def create(self, request):
        return Response(services.create_ticket(request.user, request.data), status=201)

    @action(detail=True, methods=['post'])
    def rate(self, request, pk=None):
        return Response(services.rate_ticket(request.user, pk, request.data))

router = SimpleRouter(trailing_slash=False)
router.register('api/v1/helpdesk/tickets', api.HelpdeskViewSet, basename='api-ticket')
urlpatterns += router.urls
```

The views stay thin: services hold the rules and validate. The literal port's hand-written `api_view`
(`hajiri.core.http`) and zod clone (`hajiri.core.schema`) are gone; CORS helpers live in `hajiri.core.cors`.

## 6. Views: DRF only; the pages are React

There are no server-rendered pages. Every address outside `/api/` and `/static/` answers the React app's
`index.html` (`hajiri.web.views.spa`, the last pattern in `config/urls.py`); React Router decides what to show,
including the sign-in page, role refusals and the legacy addresses. The backend's views are the DRF views of
section 5.

- **Errors.** Raise `BadRequestError`, `UnauthorizedError`, `ForbiddenError`, `NotFoundError`, `ConflictError` with
  the TS message, in services or views. DRF's `exception_handler` (and `HttpErrorMiddleware` for the few plain views)
  answer the TS JSON shape. The React app reacts to 401 (sign-in), 403 `password change required` and
  `two-step sign-in setup required` itself.
- **Database errors** need no handling: a unique, overlap, foreign-key or check violation or a trigger's
  `RAISE EXCEPTION` becomes the same 409/400 as the TS handler. Run the statement inside `transaction.atomic` (your
  service does) so the connection stays usable; catch `IntegrityError` only to give a more specific message.
- **Audit and notifications** are the service's job, exactly as in TS (`hajiri.core.audit.audit(...)`).
- **HTML the API returns** (payslips, salary sheets, letters, certificates, offer letters, review print-outs) is built
  by the service or rendered with `render_to_string` from the app's own templates; it is opened in a new tab by the
  React app, so it is a complete standalone document with inline styles only.
- Request data: `request.user` / `request.hajiri_user` (`AuthUser`), `request.auth` / `request.session_token`,
  `request.auth_method` (`'cookie' | 'bearer' | None`).

## 7. Background work: django.tasks

Background work uses Django's task framework with the database backend (`django_tasks_db`), so a single machine
still needs only PostgreSQL.

- One-off work: write a `@task` function (`from django.tasks import task`) with JSON-serialisable arguments and
  `.enqueue(...)` it **after** the business transaction commits (`transaction.on_commit(lambda: my_task.enqueue(id))`).
- Periodic and daily jobs: register a plain function in your app's `jobs.py`; the scheduler enqueues it as
  `hajiri.worker.tasks.run_job(name)` at its cadence, and an advisory lock per job keeps two workers from running it
  at once:

```python
# hajiri/helpdesk/jobs.py
from hajiri.worker.registry import daily_job

@daily_job('helpdesk')                  # after the nightly sweep, once a day at 00:30 Nepal time
def helpdesk_job(now):                  # `now` is the scheduler's tick time
    return helpdesk_daily(now)          # a JSON-able summary, stored as the task result

@periodic_job('kioskHeartbeat', every_seconds=300)
def heartbeat(now): ...
```

- Hooks between modules: `register_hook(name, fn)` / `hook(name)` (leave accrual, workflow approvers).
- Processes: `manage.py run_worker` (scheduler + task worker, the default), or `manage.py run_scheduler` plus any
  number of `manage.py db_worker`. `RUN_WORKER=true` starts both inside the web process. `run_worker --once --jobs
  queue,digest` runs jobs in-process and prints their results.
- In tests call the job function (or `run_once([...])`) directly; `run_job.call(name)` runs the task body.

## 8. Shared helpers instead of hand-written ones

| need | use |
|---|---|
| sort names / text like the browser's `localeCompare` | `hajiri.core.collation.collate_key`, `sort_by_name`, `locale_compare` (pyuca) |
| shrink a profile photo | `hajiri.core.images.square_jpeg(data)` (Pillow: 320 px square JPEG, quality 85, EXIF orientation) |
| email | Django's email framework, configured from `SMTP_URL` (`hajiri.core.mail`); notifications send with `EmailSender` |
| company settings | `hajiri.core.settings_store.get_setting`, `put_setting`, `add_setting_if_missing`, `setting_subquery` |
| QR codes | segno (`hajiri.field` kiosk QR) |
| CSV | `hajiri.web.csv.csv_response` / `to_csv` (the csv module, formula protection, BOM) |
| stored files | the encrypted file store in `hajiri.files.store` (files live encrypted in the database, not on disk); `has_signature(mime, data)` checks an upload's magic number |
| rounding for display | `hajiri.core.numbers.round_half_up(x, places)`, `to_fixed(x, digits)`, `whole(x)`; payroll amounts use the engine's Decimal helpers |
| UUID input | `hajiri.core.scoping.as_uuid` (400 'invalid value format'), `is_uuid`; `validation.parse_uuid` ('Invalid UUID') |
| pagination of lists | `hajiri.core.api.Pagination` (`?page=&pageSize=`) |

## 9. Navigation

The menus, hubs, tabs, command palette and badges are the React app's (`frontend/src/components/Layout.jsx`,
`Hub.jsx`, `lib/access.js`). Who may open a screen is decided by the endpoints it calls (`HasRole`, `HasModule`,
`FeatureEnabled`); the React route guards only hide what would be refused. Feature switches in the API:
`FeatureEnabled('expenses')`, or `hajiri.web.nav.feature_on(request, key)` (cached per request). The apps' `nav.py`
files describe the retired server-rendered pages and are not read any more.

## 10. URLs

- JSON endpoints under `/api/v1/...` with the TS path, method and parameters, so the React app and existing clients
  keep working. No trailing slash. Ids are `<str:id>` where the TS answered a malformed id with 400 'Invalid UUID'.
- Every pattern has a `name` (`api_<thing>`).
- Page addresses are the React app's routes (`frontend/src/App.jsx`); do not add Django routes for them. The careers
  site keeps named routes to `hajiri.web.views.spa` because services build public links with `reverse()`.
- `/health`, `/api/schema` and `/api/docs` are the other non-page addresses; WhiteNoise serves `/static/` and the
  React build's files (`/assets/*`, `sw.js`, `manifest.webmanifest`, icons) before URL routing.

## 11. Templates

Only for HTML the API returns as a document (`learning/certificate.html`, `hiring/print/offer_letter.html`,
`performance/review_print.html`): standalone, inline styles, no scripts, no template tag libraries, rendered with
`render_to_string`. Everything people see in the web app is React (`frontend/src`).

## 12. When raw SQL is acceptable

Raw SQL is the exception. It is allowed only where the ORM cannot express the statement well, and every remaining
statement carries a one-line `# Raw SQL: <why>` comment. Accepted cases:

- DDL the ORM has no API for (creating `punch_event` partitions) and catalog checks Django's introspection leaves out
  (`to_regclass` for partitions).
- PostgreSQL advisory locks (`pg_try_advisory_lock`), which serialise the scheduler and each job.
- Recursive CTEs (org trees) and set-returning functions where a queryset would be contorted.
- An upsert whose conflict target is not the primary key but must rewrite the primary key (`seed_bs_calendar`).

Not acceptable as reasons: "it was raw SQL in the port", conditional increments (`.filter(...).update(F() + 1)`),
upserts (`bulk_create(update_conflicts=True)`), `EXISTS`/correlated subqueries (`Exists`, `Subquery`, `OuterRef`),
JSON operators (`KT`, key lookups), `FOR UPDATE SKIP LOCKED` (`select_for_update(skip_locked=True)`), calls to
`hj_manages`/`hj_is_head` (`Manages`, `IsHead`). Raw SQL goes through `django.db.connection.cursor()` with a
`# Raw SQL:` comment saying why. Application code has 14 such statements (advisory locks, recursive CTEs, the
partition DDL and catalog check, the BS calendar upsert, the health probe, one optional install column). Tests may
state expected table contents in plain SQL with `tests.support.sql` (`rows`, `one`, `execute`); application code
never imports it.

## 13. The web app (frontend/)

- React 19, Mantine, react-router, Vite; **plain JavaScript** (`.js`/`.jsx`, no TypeScript or type annotations).
- `src/api/client.js` calls `/api/v1/...` with the session cookie and normalises errors; `src/api/*.js` and inline
  `api.get/post` calls in pages are the contract the backend implements.
- `vite build` writes `server/frontend_build/` (committed): `scripts/build-frontend.sh`. Development: Django on
  :3000 and `npm run dev` on :5173 (Vite proxies `/api` and `/health`).
- Served by Django: `index.html` with `Cache-Control: no-cache` and the TS Content-Security-Policy (`script-src
  'self'`, `style-src 'self' 'unsafe-inline'`, `img-src 'self' data: blob:`, `frame-ancestors 'none'`, ...); hashed
  `/assets/*` cached for a year (WhiteNoise, `WHITENOISE_IMMUTABLE_FILE_TEST`).

## 14. Lists, CSV, uploads

- Lists: `hajiri.core.api.Pagination` (`?page=&pageSize=` -> `{items, total, page, pageSize}`) and django-filter
  `FilterSet`s; endpoints that the TS API answered with a plain array keep doing so.
- CSV: `hajiri.web.csv.csv_response(filename, [{'key','header'}], rows)` / `to_csv` (RFC 4180, BOM, formula
  protection, same as reports/csv.ts).
- Uploads: the React app posts `/api/v1/files` (JSON data URL or multipart `file` + `purpose`, answer
  `{"file": {"id", "filename", "mime", "bytes", "purpose"}}`, max 4 MB, PDF/JPEG/PNG/WebP) and then sends the file id
  in the module's body (`fileIds`); the service attaches it. Profile photos are resized with
  `hajiri.core.images.square_jpeg`.

## 15. Worked example: the help desk, before and after

The help desk (`hajiri/helpdesk/`) was converted completely; compare `git log -p` for the full diff.

**Validation** — before (a clone of zod):

```python
TICKET_BODY = Obj({'categoryId': Uuid(), 'subject': Str(trim=True, min=3, max=160), ...})
b = parse(TICKET_BODY, body)
```

after (serializers.py, the same rules and messages):

```python
class TicketIn(serializers.Serializer):
    categoryId = serializers.UUIDField()
    subject = serializers.CharField(min_length=3, max_length=160)
b = plain(validate(TicketIn, body))
```

**Reading a ticket** — before (raw SQL, company checked by hand afterwards):

```python
t = _norm(one(f'{TICKET_SELECT} WHERE t.id = %s', [tid]))
```

after (scoped queryset with annotations; another company's id is simply not found):

```python
def tickets():
    return HelpdeskTicket.objects.annotate(confidential=F('category__confidential'),
                                           category_name=F('category__name'), requester_name=F('requester_user__name'), ...)

found = _rows(tickets().for_company(company_id_of(user)).filter(pk=tid))
```

**Conditional update** — before:

```python
execute("""UPDATE helpdesk_ticket SET first_response_at = coalesce(first_response_at, now()),
             status = CASE WHEN status = 'open' THEN 'in_progress' ELSE status END, updated_at = now() WHERE id = %s""", [id])
```

after:

```python
HelpdeskTicket.objects.filter(pk=t['id']).update(
    first_response_at=Coalesce('first_response_at', Now()),
    status=Case(When(status=Status.OPEN, then=Value(Status.IN_PROGRESS)), default=F('status'), output_field=TextField()),
    updated_at=Now())
```

**Sequential numbers** — before `pg_advisory_xact_lock(hashtext('helpdesk:' || company))`; after a row lock on the
company that does not block inserts referencing it:

```python
Company.objects.select_for_update(no_key=True).filter(pk=company_id).first()
n = (HelpdeskTicket.objects.for_company(company_id).aggregate(n=Max('number'))['n'] or 0) + 1
```

**Statistics** — `count(*) FILTER (...)` and `round(avg(extract(epoch ...)))` become one `aggregate()` with
`Count(..., filter=Q(...))` and `Avg(Extract(F('resolved_at') - F('created_at'), 'epoch') / 3600)`.

**Settings** — `INSERT ... ON CONFLICT (company_id, key) DO UPDATE` becomes `put_setting(cid, 'helpdesk.policy', policy,
updated_by=user.id)`; "seed once" becomes `add_setting_if_missing` + `bulk_create(ignore_conflicts=True)`.

**People** — agents and owners are `company_users(cid)` / `company_roles(cid, user_id)`, so members of the group with
access to the company can own topics and be assigned tickets.

**JSON API** — new `api.py`: `HelpdeskViewSet` (queue paginated, mine, create, retrieve, PATCH, messages, reopen,
close, rate), `CategoryViewSet`, `StatsViewSet`, routed under `/api/v1/helpdesk/...` and documented in `/api/docs`.

**Daily job** — the multi-table `UPDATE ... FROM ... RETURNING` becomes a loop over companies with their
`autoCloseDays` (`setting_subquery`) and a `select_for_update(of=('self',))` of breached tickets followed by one
`update()`.

## 16. Tests

Ports of the TS tests go in `server/tests/<app>/test_api_*.py`: the HTTP-level assertions of
`git show 65a9079:apps/api/test/<x>.test.ts`, against `/api/v1/...` with `django.test.Client`. Use the shared
factories:

```python
from tests.web.factories import PW, bearer, make_company, make_employee, make_user, set_features_off, signed_in_client

def test_manager_sees_only_their_team(db):
    co = make_company()
    boss = make_employee(co, name='Ram')
    other = make_employee(co)
    client, user = signed_in_client(co, roles=['manager'], employee=boss)
    r = client.post('/api/v1/regularisation', {'employeeId': str(other.id), ...}, content_type='application/json')
    assert r.status_code == 403 and r.json() == {'error': 'not allowed to access this employee'}
```

- `make_company(code=None, name=...)`, `make_employee(company, code=, name=, manager=, branch=, org_unit=, **fields)`
  (adds a current `employee_assignment`, so manager scoping works), `make_user(company, roles=, employee=, email=,
  password=PW, must_change_password=)`, `sign_in(client, user)` (real session cookie), `signed_in_client(company,
  roles=, employee=True|Employee|None)` -> `(client, user)`; `bearer(token)` headers, `set_features_off(company,
  'expenses')`, `require_two_factor_for(company, 'hr')`, `audit_actions(row_id)`, `user_row(user)`.
- Fixtures: `from tests.web.fixtures import *` in your `tests/<app>/conftest.py` gives `acme`, `other_company`,
  `employee`, `hr_client`, `employee_client`, `manager_setup` (client, user, boss, report), `make_client(roles=...)`.
- Assert what the TS test asserts: status codes, JSON bodies and field names, rows, audit actions.
- Company isolation: the outsider and the group admin working in the other company get 404 for an id of another
  company; the owner company's admin does not (`tests/multicompany`).
- Cookie writes: the test client sends no `Origin`, which passes like the TS guard; send
  `HTTP_ORIGIN='https://evil.example'` to prove a cross-site write is refused (`tests/web/test_spa.py`).
- Background jobs: call the job function or `run_once([...])`; `run_job.call(name)` runs the task body.
- Run only your tests with your own database:
  `cd server && DJANGO_TEST_DATABASE=hajiri_t_<slice> .venv/bin/pytest -q -p no:cacheprovider tests/<app>`
  (`--create-db` the first time, `--reuse-db` after).

## 17. Errors and special addresses

- Unknown addresses under `/api/` answer `{"error": "not found"}` (404); every other address is the React app.
  `config.errors` answers 400/403/404/500 as JSON.
- Static files and the React build are served by WhiteNoise (`hajiri.web.middleware.StaticFilesMiddleware`), so no
  web server is needed in front.
- Public endpoints (careers) and device endpoints authenticate with nothing or their own key; Bearer-token requests
  and signed-out requests skip the Origin check.

## 18. Sign-in and sessions

The React app signs in through `/api/v1/auth/login` (and `/login/2fa`), `/logout`, `/me`, `/change-password`,
`/2fa/*` and `/sessions/*` (`hajiri.integrations.auth_api`). The session cookie `hajiri_session` is HttpOnly,
SameSite=Lax, 12 h, Secure when `COOKIE_SECURE=true`, shared with the TS API.

