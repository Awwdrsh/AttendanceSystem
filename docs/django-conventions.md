# Django conventions (idiomatic)

The rulebook every slice follows, so the app reads as one idiomatic Django product. Follow it literally. The
foundation it describes lives in `hajiri.core` (auth, companies, scoping, validation, DRF integration, settings store,
collation, images, mail), `hajiri.db.models` (models, choices, company querysets, SQL-function expressions),
`hajiri.worker` (django.tasks), `hajiri.web` (decorators, layout, components) and `server/templates/`. The help desk
(`hajiri/helpdesk/`) is the worked example: every pattern below appears there, and section 15 shows it before and
after the conversion.

If you need something that is not here, list it in `sharedChangesNeeded`; do not write your own framework.

## 1. Files in a slice

```
server/hajiri/<app>/
  services.py     business rules: ORM queries, transactions, audit, notifications; no request objects;
                  raises hajiri.core.errors; validates TS-shaped bodies with its serializers
  serializers.py  DRF serializers: the request contract (the TS zod schemas) and JSON response shapes
  forms.py        Django forms: read page posts (HTML types, checkboxes, repeated inputs) into service input
  views.py        page and HTMX views: form -> service -> render
  api.py          DRF views/viewsets for JSON clients (only where a non-browser client calls them)
  jobs.py         background jobs registered with hajiri.worker.registry (daily/periodic)
  urls.py         urlpatterns (mounted at the site root with namespace = app name); DRF router for api.py
  nav.py          NAV entries (section 9)
server/templates/<app>/ or server/hajiri/<app>/templates/<app>/
  <page>.html     full pages: {% extends "base.html" %}
  partials/       HTMX fragments (no extends)
server/tests/<app>/
  conftest.py     from tests.web.fixtures import *   (optional)
  test_services.py, test_views.py, test_api.py
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
  `web:switch_company` with `next`), never by opening a foreign id in the active company.
- Writes go to one company at a time and only through the active company (switch first), except group
  administration in `hajiri.core.companies` (`create_group_company`, `grant_access`, `grant_group_role`, ...), which
  checks `is_group_admin` / `has_role(user, 'admin')` itself and audits every change.
- Who sees the Group hub: `hajiri.core.companies.can_open_group(user)` (`NavItem(visible=...)`); the overview:
  `can_view_group(user)`; group administration: `is_group_admin(user)`.

### Testing isolation

`tests/multicompany/conftest.py` builds a group of two companies plus one outside it, with people who reach one
company, two companies, or every company through a group role (`world` fixture). A new detail page or JSON
endpoint taking an id gets a case in `tests/multicompany/test_isolation.py` (or the same three requests in the
slice's tests): the outsider and the group admin working in the other company get 404, the owner company's admin
does not.

## 4. Validation: serializers for the contract, forms for pages

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

**Page posts** are read by **Django forms** (`forms.py`): the form knows the HTML side (checkbox -> bool, number
inputs, repeated `file_ids`, '' -> None) and builds the service input; a form error is raised as the same
`ValidationError` (`form_issues(form)`), so pages and JSON clients get the same messages. Re-render a modal form with
status 422 only where the TS page showed errors inside the form (section 13); otherwise let the error become a toast.

```python
@require_POST
@login_required
@require_feature('helpdesk')
def reply(request, ticket_id):
    message = MessageForm(request.POST).message()          # raises ValidationError on unreadable input
    services.add_message(request.hajiri_user, ticket_id, message)
    return _changed(request, ticket_id, 'Note added.' if message['internal'] else 'Message sent.')
```

Forms that stand alone (sign-in, group administration) carry their own validation and re-render with 422.

## 5. JSON endpoints: Django REST framework

Only for non-browser clients (terminals, kiosk tablets, Bearer-token integrations), under `/api/v1/...` with the TS
path. They are DRF views or viewsets; the defaults in `REST_FRAMEWORK` (config/settings.py, `hajiri.core.api`) give:

- **Authentication** `HajiriAuthentication`: the `hajiri_session` cookie (Django's CSRF check enforced) or
  `Authorization: Bearer <token>` (no CSRF). `request.user` is the `AuthUser` (active company); `request.auth` the
  session token.
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

## 6. Views (pages and HTMX)

Function views. Decorators from `hajiri.web.decorators`, always outermost:

| decorator | meaning (TS) |
|---|---|
| `@login_required` | signed in, password changed, two-step set up if the company requires it (requireAuth + requirePasswordChanged) |
| `@require_role('hr', 'manager')` | login_required + any of the roles in the active company; admin always passes. Error: `requires role hr or manager` |
| `@require_module('payroll')` | `require_role(*MODULE_ROLES['payroll'])` (AREA in lib/access.ts) |
| `@require_feature('expenses')` | the active company's feature switch is on; pages redirect home, other requests 404 `feature off` |
| `@signed_in` | any session, before the gates (only for password change, two-step setup, sign out) |

Also use `django.views.decorators.http.require_GET / require_POST / require_http_methods`.

- **Errors.** Raise `BadRequestError`, `UnauthorizedError`, `ForbiddenError`, `NotFoundError`, `ConflictError` with
  the TS message, in services or views. `HttpErrorMiddleware` turns them into JSON in the TS shape for API clients, a
  red toast for HTMX (`Not saved` for posts, `Could not load` for gets; validation issues joined as
  `from: YYYY-MM-DD; bad`) and a friendly error page otherwise. 401 sends the user to `/login?next=...`; "password
  change required" and two-step setup required go to their pages.
- **Database errors** need no handling: a unique, overlap, foreign-key or check violation or a trigger's
  `RAISE EXCEPTION` becomes the same 409/400 as the TS handler. Run the statement inside `transaction.atomic` (your
  service does) so the connection stays usable; catch `IntegrityError` only to give a more specific message.
- **Role refused on a page** lands on the first tab of the same hub the user may see, or "Not available for your role".
- **Audit and notifications** are the service's job, exactly as in TS (`hajiri.core.audit.audit(...)`).
- Request data: `request.hajiri_user` (`AuthUser`), `request.session_token`, `request.auth_method`
  (`'cookie' | 'bearer' | None`), `request.htmx` (django-htmx: `request.htmx.target`, `bool(request.htmx)`).

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
| QR codes | `{% qr_code value %}` (segno) |
| CSV | `hajiri.web.csv.csv_response` / `to_csv` (the csv module, formula protection, BOM) |
| stored files | the encrypted file store in `hajiri.files.store` (files live encrypted in the database, not on disk); `has_signature(mime, data)` checks an upload's magic number |
| rounding for display | `hajiri.core.numbers.round_half_up(x, places)`, `to_fixed(x, digits)`, `whole(x)`; payroll amounts use the engine's Decimal helpers |
| UUID input | `hajiri.core.scoping.as_uuid` (400 'invalid value format'), `is_uuid`; `validation.parse_uuid` ('Invalid UUID') |
| pagination of lists | `hajiri.web.tables.Table` on a QuerySet (pages) or `hajiri.core.api.Pagination` (JSON) |

## 9. Navigation: nav.py

The sidebar shows **hubs** (fixed list in `hajiri/web/nav.py` `HUBS`, mirroring Layout.tsx); each app adds the
**tabs** of the hubs it owns. A hub appears when at least one of its tabs is visible to the user; it links to the
first visible tab (lowest `order`); with more than one visible tab the page shows a tab strip. Every visible tab is
also a command palette entry (Cmd/Ctrl+K).

```python
# hajiri/attendance_tools/nav.py
from hajiri.web.nav import NavItem, area

NAV = [
    NavItem(hub='attendance', label='Issues', url_name='attendance_tools:issues', order=120,
            roles=('manager', *area('attendance')), icon='alert-triangle',
            palette_label='Attendance issues and reminders'),
    NavItem(hub='attendance', label='Late penalties', url_name='attendance_tools:penalties', order=130,
            module='attendance', icon='clock-hour-4', palette_label='Late penalties and attendance rules'),
    NavItem(hub='attendance', label='Upload', url_name='attendance_tools:upload', order=140, module='attendance',
            icon='clock-edit', palette_label='Upload attendance (CSV)'),
]
```

Fields: `hub`, `label` (tab label from hubs.tsx), `url_name` (no arguments), `order` (use the hubs.tsx position x 10),
`roles` (tab `roles`), `module` (an AREA name instead of roles), `feature` (tab `feature`), `employee_only`
(`show: !!user?.employeeId`), `icon`, `keywords`, `palette_label` (the JUMPS label in Layout.tsx when different),
`visible` (an extra `Callable[[AuthUser], bool]`, e.g. `can_open_group` or `is_group_admin` for the Group hub).

Tab `roles` here only hide the tab; **the view's decorator enforces access** (the App.tsx route guard roles plus the
tab roles). Single-page hubs (Home `/`, Approvals, Overview, Insights, Tasks, Help desk, Notifications) register one
NavItem.

Hub keys: `home`, `my_attendance`, `time_off`, `timesheet`, `payslips`, `claims`, `growth`, `tasks`, `help`,
`people`, `engage`, `group`, `overview`, `approvals`, `employees`, `attendance`, `leave`, `payroll`, `expenses`, `performance`,
`hiring`, `helpdesk`, `relations`, `insights`, `reports`, `settings`, `notifications`, `profile`.

Counters: an app's nav.py may define `BADGES = {'approvals': lambda user: n}` (workflow) or `{'notifications': ...}`
(notifications: the red dot on the bell). Keep them one cheap query.

Feature switches in views and templates: `hajiri.web.nav.feature_on(request, 'expenses')` (cached per request).

## 10. URLs

- Pages keep the React route paths (`apps/web/src/App.tsx`, `pages/hubs.tsx`): `/attendance/exceptions`,
  `/settings/shifts`, `/me/leave`, `/employees/<uuid:id>`. No trailing slash: `path('attendance/exceptions', ...)`.
- HTMX fragments and form posts that are not pages go under `/_/<app>/...`: `path('_/shifts/<uuid:id>/delete', ...)`.
- JSON endpoints only where a non-browser client calls them (devices, kiosk tablet background calls, Bearer-token
  integrations). Put them under `/api/v1/...` with the TS path, so existing clients keep working.
- Every pattern has a `name`; refer to pages with `{% url 'shifts:list' %}` and `reverse('shifts:list')`.
  Names: `list`, `detail`, `new`, `edit`, `delete`, or the tab's slug (`exceptions`, `overtime`).

## 11. Templates

Every page extends `base.html` (the signed-in shell: sidebar, hub header and tabs, command palette, toasts). Pages
without the shell (public careers, kiosk, sign-in) extend `bare.html`. The `hj` tag library is a builtin: no
`{% load %}` needed (load `static` yourself when you need it).

```django
{% extends "base.html" %}
{% block title %}Shifts · Hajiri{% endblock %}
{% block content %}
{% page_header "Shifts" subtitle="Working hours, grace and breaks. Rosters assign them to people." %}
  <button class="hj-btn" hx-get="{% url 'shifts:new' %}" hx-target="#hj-modal-body"><i class="ti ti-plus" aria-hidden="true"></i>New shift</button>
{% endpage_header %}
{% data_table table %}
{% endblock %}
```

- **Hub pages.** If the URL is one of the hub's tabs (section 9), `base.html` draws the hub title and the tab strip
  above your content, and `page_header` automatically drops its own title, keeping only subtitle and actions (the
  React `EmbeddedContext`). So always call `page_header` with the TS page title; it does the right thing.
- `{% block page_class %}wide{% endblock %}` removes the 1240 px max width (salary sheets, rosters).
- Dates are **always** Bikram Sambat: `{{ d|bs_date }}` (28 Ashwin 2083), never `|date`.
- Money is **always** `{{ amount|npr }}` (NPR 12,34,567.00) or `|money` (no currency); pass Decimal or string.
- Icons: `<i class="ti ti-calendar" aria-hidden="true"></i>` or `{% icon "calendar" %}`, names from tabler.io/icons
  (the `@tabler/icons-react` name without `Icon`, kebab-case: `IconClockHour4` -> `clock-hour-4`).
- No inline `<script>` and no `onclick=` (the CSP blocks them). Behaviour comes from HTMX attributes, Alpine
  (`x-data`, `@click`, `x-show`) and the `data-*` hooks in `static/js/app.js` listed below.
- Text: English, sentence case, plain words; copy the TS strings exactly (messages, labels, empty states).

### Filters

| filter | example |
|---|---|
| `bs_date` | `2026-10-14` -> `28 Ashwin 2083` (date, datetime in NPT, or ISO string; Gregorian outside the table) |
| `bs_date_long` | `Wed, 28 Ashwin 2083` |
| `bs_date_short` | `28 Ashwin` this BS year, else with the year |
| `bs_datetime` | instant -> `5 Ashwin 2083, 10:17` (Nepal time) |
| `npt_time` | instant -> `10:17` |
| `dual_date` | `12 Bhadra 2083` + muted `28 Aug 2026` (HTML) |
| `gregorian` | `28 Aug 2026` |
| `bs_month` | `{{ 2083\|bs_month:6 }}` or `{{ run\|bs_month }}` (has bs_year, bs_month) -> `Ashwin 2083` |
| `fiscal_label` | `2083/84` |
| `npr`, `money` | `NPR 12,34,567.00`, `{{ v\|npr:0 }}` whole rupees; negative `−NPR 40,000.00`; None -> `—` |
| `num` | `1,234` |
| `minutes_hm` | `503` -> `8 h 23 m` |
| `pct` | `50%` |
| `relative_time` | `5 min ago`, `3 h ago`, `4 d ago` |
| `devanagari` | `२०८३-०५-१२` |
| `status_label`, `tone` | `OnLeave` -> `On leave`, tone `info` |
| `initials`, `role_label`, `truncate_text:40`, `json_attr`, `get_item:key`, `attr:"a.b"`, `to_json` | helpers |

Python equivalents are in `hajiri.web.format` (`fmt_date`, `fmt_money`, `fmt_datetime`, `minutes_to_hm`, ...): use
them in services that build text (notifications, CSV cells, PDFs).

### Components

Block components (body between the tags):

| tag | React |
|---|---|
| `{% page_header "Title" subtitle=".." back="/employees" back_label="Employees" eyebrow=".." %}actions{% endpage_header %}` | PageHeader |
| `{% section "Title" hint=".." actions=html %}content{% endsection %}` | SectionTitle + content |
| `{% card "Title" hint=".." padding="lg" cls=".." %}..{% endcard %}` (title optional) | Paper withBorder |
| `{% modal "id" "Title" size="sm\|lg\|xl" %}..{% endmodal %}`; open with `data-modal-open="id"` | Modal |
| `{% drawer "id" "Title" size="lg" %}..{% enddrawer %}` | Drawer |
| `{% alert "ok\|warn\|bad\|info" title=".." %}text{% endalert %}` | Alert |

Simple components:

| tag | React |
|---|---|
| `{% status_badge row.status late=row.late_minutes remote=row.remote %}` | StatusBadge (dot + word, every status in `STATUS`) |
| `{% badge "Verified" "ok" %}` (third argument True adds a dot) | Badge |
| `{% empty_state title=".." hint=".." action_url=".." action_label=".." icon="inbox" compact=True %}` | EmptyState |
| `{% stat_strip stats %}` with `[{'label','value','hint','tone','url'}]` | StatStrip / KpiTiles |
| `{% field "PAN" value mono=True tip=".." %}` inside `<div class="hj-fields">` | Field |
| `{% data_table table %}` | DataTable (section 14) |
| `{% report_table columns rows %}` with columns `{'key','header','type': text\|number\|date\|bool}` | ReportTable |
| `{% form_field form.name %}`, `{% form_errors form %}` | inputs with label, description, error |
| `{% bs_date_input "from" value=.. label=".." required=True min=.. max=.. holidays={iso: name} %}` | BsDatePicker / DateField; posts ISO AD |
| `{% bs_month_input "bs_year" "bs_month" year=2083 month=6 label="Pay period" %}` | BsMonthPicker |
| `{% employee_picker "employee_id" value=.. label="Employee" status="active\|exited\|all" required=True %}` | EmployeePicker |
| `{% file_upload "file_id" "expense_receipt" label="Attach receipt" value=.. filename=.. %}` | FileUpload |
| `{% qr_code value 188 "label" %}` | QrCode (server-side SVG) |
| `{% camera_capture name="photo" title=".." confirm_label="Clock in" id="cam" submit=True %}` inside a form | CameraCapture |
| `{% approval_chain "leave" employee_id=.. amount=.. %}` | ApprovalChainPreview (needs `workflow:preview`) |
| `{% chart_card "Headcount" subtitle=".." legend=legend table=table csv_url="?export=headcount" empty="No data yet" %}chart{% endchart_card %}` | ChartCard (legend shown with 2+ entries; Chart/Table switch when `table` is given) |
| `{% column_chart labels series money=True %}`, `{% line_chart labels series suffix="%" %}`, `{% donut data center_label="people" %}`, `{% hbars data %}`, `{% trend_bars points "Attendance" %}`, `{% sparkline values %}` | charts/Charts.tsx, Kpi TrendBars |
| `{% confirm_attrs "Delete this shift? Rosters using it stop working." label="Delete" %}` on an hx-post button | ConfirmDialog |
| `{% qs page=2 %}` | the current query string with changes |

chart_card `legend`: `[{'label', 'color'}]`; `table`: `{'columns': [str], 'rows': [[label, number|None, ...]]}`.
`series` for charts: `[{'key', 'label', 'values': [number|None], 'color'?}]`; donut `[{'label', 'count'}]`;
hbars `[{'label', 'value'}]`; trend points `[{'date': iso, 'value': number|None}]`.

### CSS classes

Use these instead of writing CSS (all in `static/css/hajiri.css`; colours are CSS variables, so dark mode works):

- Layout: `hj-stack` (column, `--gap`), `hj-row` (wrapping row; `nowrap`), `hj-between`, `hj-end`, `hj-grow`,
  `hj-grid` with `style="--cols:3"` (one column on phones), `hj-gap-xs|sm|md|lg`, `hj-mt`, `hj-mt-sm`, `hj-mb`.
- Text: `hj-muted`, `hj-small`, `hj-sm`, `hj-strong`, `mono`, `num`, `hj-right`, `hj-truncate`, `hj-eyebrow`,
  `hj-ok-text|warn-text|bad-text`.
- Buttons: `hj-btn` (primary), plus `default`, `light`, `subtle`, `danger`, `gray`; sizes `xs`, `sm`, `lg`; `block`,
  `icon`. `hj-iconbtn` for icon-only, `hj-link-btn` for text buttons. Buttons show a spinner while their HTMX request
  runs.
- Forms: `hj-form` (column), `hj-form-row` (2 columns, `--cols`), `hj-fieldset`, `hj-label` (+ `<span class="req">*</span>`),
  `hj-help`, `hj-error`, `hj-input` (`lg`), `hj-select`, `hj-check`, `hj-switch`, `hj-input-wrap` (icon left),
  `hj-filters` (filter bar above a table).
- Panels: `hj-card` (`p-sm|md|lg|xl|0`), `hj-fields`, `hj-kv` (dl), `hj-timeline`, `hj-progress`, `hj-skeleton`.
- Tabs inside a page: `hj-tabs` + `hj-tab` (first level), `hj-pills` + `hj-pill` (second level, inside a hub tab);
  mark the current one with `aria-current="page"` (links) or `aria-selected="true"` (Alpine buttons).
- Badges: `hj-badge hj-tone-ok|warn|bad|info|idle|accent` (`sm`), `hj-dot`, `hj-count`.
- Tables without the DataTable helper: `<div class="hj-table-wrap"><div class="hj-table-scroll"><table class="hj-table">`;
  cells `right`, `mono`, `muted`; `<tr data-href="/x">` makes a row clickable.

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

## 13. HTMX patterns

Setup done for you in `_document.html`: the CSRF header on `<body hx-headers>`, `responseHandling` so that 2xx and
**422** responses swap and other 4xx/5xx do not (they show a toast instead).

**Partial vs page.** A view answers HTMX with a fragment and a normal request with the full page:

```python
from hajiri.web.htmx import is_htmx, htmx_target
if is_htmx(request):
    return render(request, 'shifts/partials/row.html', ctx)
return render(request, 'shifts/list.html', ctx)
```

**Forms in a modal** (the common case: new/edit):

```django
<button class="hj-btn" hx-get="{% url 'shifts:new' %}" hx-target="#hj-modal-body">New shift</button>
```

The GET answers a fragment whose first element names the modal: `<form data-modal-title="New shift"
data-modal-size="lg" hx-post="{% url 'shifts:new' %}" hx-target="this" hx-swap="outerHTML" class="hj-form">`.
The shared modal opens itself after the swap. On POST:

```python
form = ShiftForm(request.POST)
if not form.is_valid():
    return render(request, 'shifts/partials/form.html', {'form': form}, status=422)   # swapped in place, errors shown
try:
    services.create_shift(user, form.cleaned_data)
except BadRequestError as e:                       # only if the TS page showed it inside the form
    return render(request, 'shifts/partials/form.html', {'form': form, 'error': e.message}, status=422)
resp = HttpResponse(status=204)
close_modal(resp); refresh(resp, 'shifts')        # close the modal, reload the table with id "shifts"
return toast(request, resp, 'Shift saved.')
```

Helpers in `hajiri.web.htmx`: `toast(request, response, message, tone='ok'|'info'|'warn'|'err', title=None)`,
`trigger(response, event, detail)`, `close_modal(response)`, `refresh(response, target_id)` (anything with
`hx-trigger="hj:refresh-<id> from:body"` reloads; data tables listen already), `hx_redirect(request, url)`
(full-page redirect for HTMX and normal requests), `flash(response, ...)` (toast on the next page after a redirect),
`no_swap(response)`, `is_htmx`, `htmx_target`. Toast texts and titles: copy the TS `toast.ok/err/info` calls
(`toast.err(e, 'Not saved')` -> `tone='err', title='Not saved'`).

**Out-of-band updates**: add `hx-swap-oob="true"` to an element with an id in the response to replace it too (e.g. a
count in the header).

**Confirmation** before destructive actions: `{% confirm_attrs "Message that spells out what changes." label="Delete" %}`
on the element with `hx-post` (styled dialog via `hx-confirm`). For a plain `<form method="post">` use
`data-confirm="..." data-confirm-label="Delete"`.

**Inline row actions**: `<button hx-post="{% url 'leave:approve' r.id %}" hx-target="closest tr" hx-swap="outerHTML">`.

**Filters**: `<form class="hj-filters" hx-get="" hx-target="#leave-requests" hx-push-url="true"
hx-trigger="change, input delay:300ms from:input[type=search]">` around the inputs; the view reads `request.GET`.

**Polling** (live boards): `hx-trigger="every 30s"`.

**Navigation stays plain links** (full page loads); do not `hx-boost` pages.

**Alpine** for local UI state only (open/closed, tabs, live character counts), never for data loading. Shared
components: `hjDate`, `hjCombo`, `hjUpload`, `hjCamera` (registered in `app.js`).

`app.js` hooks: `data-modal-open="id"`, `data-modal-close`, `data-copy="text"`, `data-download-text="element-id"
data-filename="x.txt"`, `data-palette-open`, `data-theme-toggle`, `data-tip="tooltip text"` (any element, used by
charts), `<tr data-href>`. JS API: `Hajiri.toast(msg, tone, title)`, `Hajiri.confirm({message, title, label}, fn)`,
`Hajiri.bs.fmt(iso)`, `Hajiri.bs.toAd(y, m, d)`, `Hajiri.bs.fromAd(iso)`, `Hajiri.todayNpt()`,
`Hajiri.offline.queue(userId, 'in'|'out')`, `Hajiri.deviceToken()` / `Hajiri.existingDeviceToken()` /
`Hajiri.deviceHeaders()` (lib/deviceToken.ts).

**Clock-in device token.** Put `data-device-token` on a clock-in form or button (or a parent): its HTMX requests
carry `X-Device-Token` when this browser has a token, as the TS clock-in calls did. A registration form with an
empty `<input type="hidden" name="device_token">` gets a new token filled in (and kept in local storage).

**Offline clock-ins.** A clock-in that fails for lack of connection is queued with `Hajiri.offline.queue(userId,
direction)`; the banner counts them. When the browser is back online `app.js` POSTs each one as JSON `{id, direction,
at, localDate, userId}` to the URL named `regularisation:offline_flush` (put on `<body data-offline-flush-url>` by
the layout once that URL exists). Answer 2xx when accepted (it becomes a missed-punch correction with the reason
`Clocked <in|out> at <HH:MM> while offline (time from my phone); sent automatically when the connection came back.`),
4xx with `{"error": "..."}` to drop it with that message, 5xx to keep it for the next try.

**Popup notices** (Layout.tsx PopupNotices): when the URL name `relations:popup_notices` exists, every signed-in
page loads it once (`hx-get`, `hx-trigger="load"`, `hx-swap="outerHTML"` on `#hj-popup-notices`). Answer an empty
200 when there is nothing to show, or a fragment such as a `{% modal ... open=True %}`.

## 14. Tables, search, sorting, pagination, CSV, uploads

```python
from hajiri.web.tables import Column, Table, table_response

COLUMNS = [
    Column('emp_code', 'Code', sortable=True, mono=True),
    Column('name_en', 'Name', sortable=True, link=lambda r: f"/employees/{r['id']}"),
    Column('join_date', 'Joined', format='bs_date', sortable=True),
    Column('status', 'Status', format='status'),
    Column('gross', 'Gross', format='npr', sortable=True),                  # numbers align right by themselves
    Column('actions', '', template='employees/partials/row_actions.html', csv=False),
]
table = Table(request, rows, COLUMNS, id='employees', search=('emp_code', 'name_en'), default_sort='emp_code',
              page_size=25, export='employees', empty_title='No employees yet', empty_hint='Add the first one.')
return table_response(request, table, 'employees/list.html', {'table': table, ...})
```

- `rows`: list of dicts/objects or a QuerySet (then search, sort and paging run in SQL).
- `format`: `bs_date`, `bs_date_long`, `bs_date_short`, `bs_datetime`, `time`, `relative`, `npr`, `money`, `num`,
  `minutes`, `pct`, `yesno`, `status`, or a function of the value; `render=` a function of the row (return
  `format_html(...)` for HTML); `template=` renders with `row` and `value`.
- `csv=` a function of the row for the export value (default: the raw value; dates as ISO), `False` to leave out.
- Query parameters are prefixed with the table id (`?employees-q=`, `-sort=`, `-dir=`, `-page=`, `-export=csv`), so
  two tables on one page work. The table reloads itself over HTMX and pushes the URL.
- `selectable=True, bulk_actions='app/partials/bulk.html'`: checkboxes named `<id>-selected`; bulk buttons use
  `hx-include="#<id> [data-row-check]:checked"`.
- `empty_search_title=` / `empty_search_hint=`: what an empty search shows (default 'Nothing matches that search.').
- The table wrapper sets `hx-swap="outerHTML"` and `hx-push-url="true"`, and HTMX passes both on to buttons inside
  rows. A row action that opens a dialog or drawer sets `hx-swap="innerHTML" hx-push-url="false"` itself.
- CSV for other exports: `hajiri.web.csv.csv_response(filename, [{'key','header'}], rows)` (RFC 4180, BOM, formula
  protection, same as reports/csv.ts).

### File uploads

`{% file_upload "receipt_file_id" "expense_receipt" %}` uploads at once to the files app (`files:upload`, POST
multipart `file` + `purpose`, answer `{"file": {"id", "filename", "mime", "bytes", "purpose"}}`, max 4 MB, PDF/JPEG/
PNG/WebP) and puts the stored file id in the hidden input; your form then posts just the id and your service
attaches it (the TS `fileIds` flow). For a direct upload in your own form (CSV imports), use `<input type="file">`
with `hx-encoding="multipart/form-data"` and read `request.FILES`.

Profile photos sent without the page script are resized on the server with `hajiri.core.images.square_jpeg`.

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

**Page posts** — before, views assembled bodies by hand (`_int`, `_checked`, `getlist`); after, `forms.py`
(`TicketForm(request.POST).ticket()`, `SettingsForm(...).policy()`).

**JSON API** — new `api.py`: `HelpdeskViewSet` (queue paginated, mine, create, retrieve, PATCH, messages, reopen,
close, rate), `CategoryViewSet`, `StatsViewSet`, routed under `/api/v1/helpdesk/...` and documented in `/api/docs`.

**Daily job** — the multi-table `UPDATE ... FROM ... RETURNING` becomes a loop over companies with their
`autoCloseDays` (`setting_subquery`) and a `select_for_update(of=('self',))` of breached tickets followed by one
`update()`.

## 16. Worked example: a hub tab with a table, a modal form and a delete

```python
# hajiri/shifts/urls.py
urlpatterns = [
    path('settings/shifts', views.shift_list, name='list'),
    path('_/shifts/new', views.shift_new, name='new'),
    path('_/shifts/<uuid:id>/delete', views.shift_delete, name='delete'),
]

# hajiri/shifts/nav.py
NAV = [NavItem(hub='settings', label='Shifts', url_name='shifts:list', order=20, roles=('hr',), icon='clock-hour-4')]

# hajiri/shifts/views.py
COLUMNS = [
    Column('name', 'Name', sortable=True),
    Column('start_time', 'Starts', mono=True),
    Column('grace_minutes', 'Grace', render=lambda r: f"{r['grace_minutes']} min"),
    Column('id', '', template='shifts/partials/row_actions.html', csv=False, align='right'),
]

@require_GET
@require_role('hr')
def shift_list(request):
    rows = services.list_shifts(company_id_of(request.hajiri_user))
    table = Table(request, rows, COLUMNS, id='shifts', search=('name',), default_sort='name', export='shifts',
                  empty_title='No shifts yet', empty_hint='Add the working hours people follow.')
    return table_response(request, table, 'shifts/list.html', {'table': table})

@require_http_methods(['GET', 'POST'])
@require_role('hr')
def shift_new(request):
    form = ShiftForm(request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            services.create_shift(request.hajiri_user, form.cleaned_data)
            resp = HttpResponse(status=204)
            close_modal(resp); refresh(resp, 'shifts')
            return toast(request, resp, 'Shift saved.')
        return render(request, 'shifts/partials/form.html', {'form': form}, status=422)
    return render(request, 'shifts/partials/form.html', {'form': form})

@require_POST
@require_role('hr')
def shift_delete(request, id):
    services.delete_shift(request.hajiri_user, id)        # raises NotFoundError / ConflictError with TS messages
    return toast(request, refresh(HttpResponse(status=204), 'shifts'), 'Shift deleted.')
```

```django
{# templates/shifts/list.html #}
{% extends "base.html" %}
{% block content %}
{% page_header "Shifts" subtitle="Working hours, grace and breaks." %}
  <button class="hj-btn" hx-get="{% url 'shifts:new' %}" hx-target="#hj-modal-body"><i class="ti ti-plus" aria-hidden="true"></i>New shift</button>
{% endpage_header %}
{% data_table table %}
{% endblock %}

{# templates/shifts/partials/form.html #}
<form class="hj-form" data-modal-title="New shift" hx-post="{% url 'shifts:new' %}" hx-target="this" hx-swap="outerHTML">
  {% form_errors form %}
  {% form_field form.name %}
  <div class="hj-form-row">{% form_field form.start_time %}{% form_field form.end_time %}</div>
  <div class="hj-modal-foot"><button type="button" class="hj-btn default" data-modal-close>Cancel</button><button class="hj-btn" type="submit">Save</button></div>
</form>

{# templates/shifts/partials/row_actions.html #}
<button class="hj-btn danger subtle xs" hx-post="{% url 'shifts:delete' row.id %}"
        {% confirm_attrs "Delete this shift? Rosters that use it stop working." label="Delete" %}>Delete</button>
```

```python
# tests/shifts/test_views.py
def test_hr_adds_a_shift(db):
    co = make_company()
    c, _ = signed_in_client(co, roles=['hr'])
    assert b'No shifts yet' in c.get('/settings/shifts').content
    r = c.post('/_/shifts/new', {'name': 'Day', 'start_time': '10:00', 'end_time': '18:00'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Shift saved.'
    assert 'hj:refresh-shifts' in hx_triggers(r)
    assert signed_in_client(co, roles=['employee'])[0].get('/settings/shifts').status_code == 403
```

## 17. Tests

Ports of the TS tests go in `server/tests/<app>/`. Use the shared factories:

```python
from tests.web.factories import (PW, bearer, htmx, make_company, make_employee, make_user, set_features_off,
                                 sign_in, signed_in_client, toasts, hx_triggers, audit_actions)

def test_manager_sees_only_their_team(db):
    co = make_company()
    boss = make_employee(co, name='Ram')
    mine = make_employee(co, manager=boss)
    other = make_employee(co)
    client, user = signed_in_client(co, roles=['manager'], employee=boss)
    r = client.get('/attendance')
    assert r.status_code == 200
    r = client.post(f'/_/regularisation/{other.id}/fix', {...}, **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'not allowed to access this employee'
```

- `make_company(code=None, name=...)`, `make_employee(company, code=, name=, manager=, branch=, org_unit=, **fields)`
  (adds a current `employee_assignment`, so manager scoping works), `make_user(company, roles=, employee=, email=,
  password=PW, must_change_password=)`, `sign_in(client, user)` (real session cookie), `signed_in_client(company,
  roles=, employee=True|Employee|None)` -> `(client, user)`.
- Request helpers: `htmx(target=None, current_url=None)` headers, `bearer(token)` headers; response helpers
  `toasts(response)` (HX-Trigger or flash cookie), `hx_triggers(response)`; `set_features_off(company, 'expenses')`,
  `require_two_factor_for(company, 'hr')`, `audit_actions(row_id)`, `user_row(user)`.
- Fixtures: `from tests.web.fixtures import *` in your `tests/<app>/conftest.py` gives `acme`, `other_company`,
  `employee`, `hr_client`, `employee_client`, `manager_setup` (client, user, boss, report), `make_client(roles=...)`.
- Assert what the TS test asserts: status codes, messages, rows, audit actions. View tests go through
  `django.test.Client`; pages are 200 with the expected text, HTMX endpoints return fragments, toasts and
  HX-Trigger events. `Client(enforce_csrf_checks=True)` when testing CSRF.
- Groups of companies: `hajiri.core.companies.create_group_company(admin, {'code', 'name_en'})`,
  `grant_access(admin_in_that_company, {'email', 'roles', 'employee_id'})` and `auth.switch_company(user, token, cid)`;
  `tests/core/test_multi_company.py` shows a full group fixture. Test that another company's ids are not found.
- JSON endpoints: `Client().get(url, **bearer(token))`; cookie posts need the CSRF token
  (`Client(enforce_csrf_checks=True)` to prove it is enforced).
- Background jobs: call the job function or `run_once([...])`; `run_job.call(name)` runs the task body; the
  scheduler test in `tests/worker` shows enqueue and execution.
- Run only your tests with your own database:
  `cd server && DJANGO_TEST_DATABASE=hajiri_t_<slice> .venv/bin/pytest -q -p no:cacheprovider tests/<app>`
  (`--create-db` the first time, `--reuse-db` after).

## 18. Errors and pages for special cases

- 404/403/500 templates exist; raise the typed errors and let the middleware answer.
- Static files are served by WhiteNoise (`hajiri.web.middleware.StaticFilesMiddleware`): from `static/` directly
  until `collectstatic` has filled `staticfiles/`, so no web server is needed in front. Reference them with
  `{% static %}`.
- `bare.html` for public pages (careers, kiosk). Public endpoints that are posted by other sites' browsers (none
  today) would need `@csrf_exempt`; device endpoints authenticate with their own key and are `@csrf_exempt` with
  a comment saying why.
- Bearer-token requests skip CSRF automatically.

## 19. Sign-in and sessions (owned by web)

`/login` (with the two-step step), `/login/2fa`, `/logout` (POST), `/change-password`, `/setup-two-step`,
`/me/profile/security` (sessions, recovery codes, turn off), `/_/web/me` (JSON). The session cookie
`hajiri_session` is HttpOnly, SameSite=Lax, 12 h, Secure when `COOKIE_SECURE=true`, shared with the TS API.
`/login` and `/login/2fa` also accept JSON (`{"email","password"}`) and answer like the TS API.
