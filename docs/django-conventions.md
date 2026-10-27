# Django conventions

How every slice writes its pages, so the app reads as one product. Follow this literally. The component gallery at
`/_/web/components` (sign in first) shows every component live; its source is `server/templates/web/components.html`.

Everything here lives in `hajiri.web` (Python helpers and template tags), `server/templates/` (layouts and
components) and `server/static/` (CSS, JS, vendored HTMX 2.0.4, Alpine 3.14.8, Tabler icons 3.31, Manrope and
JetBrains Mono). If you need something that is not here, list it in `sharedChangesNeeded`; do not write your own CSS
framework or JS in your app.

## 1. Files in a slice

```
server/hajiri/<app>/
  services.py     business rules, ported from the TS service/routes; no request objects; raises hajiri.core.errors
  views.py        function views: parse input (Django forms), call services, render
  forms.py        Django forms
  urls.py         urlpatterns (already mounted at the site root with namespace = app name)
  nav.py          NAV entries (section 6)
server/templates/<app>/
  <page>.html     full pages: {% extends "base.html" %}
  partials/       HTMX fragments (no extends)
server/tests/<app>/
  conftest.py     from tests.web.fixtures import *   (optional)
  test_services.py, test_views.py
```

Templates go in `server/templates/<app>/...` (the shared `DIRS`) or in `server/hajiri/<app>/templates/<app>/...` (loaded
by `APP_DIRS`); several slices use the second form. Either way the template name is `<app>/...`, so a file can move
between the two without a code change.

## 2. URLs

- Pages keep the React route paths (`apps/web/src/App.tsx`, `pages/hubs.tsx`): `/attendance/exceptions`,
  `/settings/shifts`, `/me/leave`, `/employees/<uuid:id>`. No trailing slash: `path('attendance/exceptions', ...)`.
- HTMX fragments and form posts that are not pages go under `/_/<app>/...`: `path('_/shifts/<uuid:id>/delete', ...)`.
- JSON endpoints only where a non-browser client calls them (devices, kiosk tablet background calls, Bearer-token
  integrations). Put them under `/api/v1/...` with the TS path, so existing clients keep working.
- Every pattern has a `name`; refer to pages with `{% url 'shifts:list' %}` and `reverse('shifts:list')`.
  Names: `list`, `detail`, `new`, `edit`, `delete`, or the tab's slug (`exceptions`, `overtime`).

## 3. Views

Function views. Decorators from `hajiri.web.decorators`, always outermost:

| decorator | meaning (TS) |
|---|---|
| `@login_required` | signed in, password changed, two-step set up if the company requires it (requireAuth + requirePasswordChanged) |
| `@require_role('hr', 'manager')` | login_required + any of the roles; admin always passes. Error: `requires role hr or manager` |
| `@require_module('payroll')` | `require_role(*MODULE_ROLES['payroll'])` (AREA in lib/access.ts) |
| `@require_feature('expenses')` | the company feature switch is on; pages redirect home, other requests 404 `feature off` |
| `@signed_in` | any session, before the gates (only for password change, two-step setup, sign out) |

Also use `django.views.decorators.http.require_GET / require_POST / require_http_methods`.

```python
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from hajiri.core.auth import assert_manages, company_id_of, has_role
from hajiri.web.decorators import require_feature, require_role
from hajiri.web.htmx import toast, hx_redirect
from hajiri.web.tables import Column, Table, table_response

from . import services
from .forms import ShiftForm


@require_GET
@require_role('hr')
def shift_list(request):
    company = company_id_of(request.hajiri_user)          # ALWAYS scope by company
    table = Table(request, services.list_shifts(company), COLUMNS, id='shifts', search=('name',), default_sort='name')
    return table_response(request, table, 'shifts/list.html', {'table': table})
```

Rules:

- **Company scoping.** Every query filters by `company_id_of(request.hajiri_user)`. A record of another company is
  `NotFoundError` (never 403), so ids cannot be probed across tenants.
- **Manager scoping.** Use `assert_manages(user, employee_id, area=None)` before showing or acting on one employee,
  and `manages_sql('%s', 'e.id')` in list queries (see `hajiri.web.views.search_employees` for the pattern).
- **Errors.** Raise `BadRequestError`, `UnauthorizedError`, `ForbiddenError`, `NotFoundError`, `ConflictError` with
  the TS message, in services or views. `HttpErrorMiddleware` turns them into: JSON in the TS shape for API
  clients (Bearer or `Accept: application/json`): `{"error"}`, plus `"details"` when there are any,
  `{"error": "validation", "issues": [{path, message}]}` for `hajiri.core.errors.ValidationError` (what
  `hajiri.core.schema.parse` raises), `{"error": "feature off", "feature"}`; for HTMX a red toast (`Not saved` for
  posts, `Could not load` for gets) with no swap, validation issues joined as `from: YYYY-MM-DD; bad`; for a page a
  friendly error page. 401 sends the user to `/login?next=...`; "password change required" and two-step setup
  required go to their pages. Do not catch these errors just to re-render, except where the TS page showed the message
  inline in the form (then re-render with status 422, section 5).
- **Database errors** need no handling either: a unique, overlap, foreign-key or check violation, a bad value format
  or a trigger's `RAISE EXCEPTION` that escapes the view becomes the same 409/400 as the TS error handler
  (`already exists`, `overlaps an existing period`, `referenced record does not exist`, `value not allowed`, ...).
  Run the failing statement inside `transaction.atomic` (your service does anyway) so the connection stays usable.
- **Role refused on a page.** A GET page that raises `requires role ...` lands on the first tab of the same hub the
  user may see (Hub.tsx); if there is none it shows "Not available for your role" (RequireRole.tsx). A signed-in
  browser asking for an address no app serves is sent to `/` (the App.tsx catch-all); API clients and `/_/...` get
  404.
- **Audit and notifications** are the service's job, exactly as in TS (`hajiri.core.audit.audit(...)`).
- **Transactions.** Wrap a mutation in `transaction.atomic` in the service, except steps that count failures.
- **Never trust ids from the form**: re-read them scoped by company.
- Request data: `request.hajiri_user` (an `AuthUser`: `id`, `company_id`, `employee_id`, `email`, `name`, `roles`),
  `request.session_token`, `request.auth_method` (`'cookie' | 'bearer' | None`).

## 4. Templates

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

- **Hub pages.** If the URL is one of the hub's tabs (section 6), `base.html` draws the hub title and the tab strip
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
| `{% data_table table %}` | DataTable (section 7) |
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

## 5. HTMX patterns

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

## 6. Navigation: nav.py

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
(`show: !!user?.employeeId`), `icon`, `keywords`, `palette_label` (the JUMPS label in Layout.tsx when different).

Tab `roles` here only hide the tab; **the view's decorator enforces access** (the App.tsx route guard roles plus the
tab roles). Single-page hubs (Home `/`, Approvals, Overview, Insights, Tasks, Help desk, Notifications) register one
NavItem.

Hub keys: `home`, `my_attendance`, `time_off`, `timesheet`, `payslips`, `claims`, `growth`, `tasks`, `help`,
`people`, `engage`, `overview`, `approvals`, `employees`, `attendance`, `leave`, `payroll`, `expenses`, `performance`,
`hiring`, `helpdesk`, `relations`, `insights`, `reports`, `settings`, `notifications`, `profile`.

Counters: an app's nav.py may define `BADGES = {'approvals': lambda user: n}` (workflow) or `{'notifications': ...}`
(notifications: the red dot on the bell). Keep them one cheap query.

Feature switches in views and templates: `hajiri.web.nav.feature_on(request, 'expenses')` (cached per request).

## 7. Tables, search, sorting, pagination, CSV

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

## 8. Forms

Django forms mirroring the zod schemas (same max lengths, choices, required fields). Messages for API clients come from
the service errors; for the page, render fields with `{% form_field form.x %}` and form-wide errors with
`{% form_errors form %}`. `hajiri.web.forms.first_error(form)` gives one line `Label: message` (the API's issue
list style) for toasts.

- Dates: the form field is `forms.DateField()` and the template uses `{% bs_date_input "field" value=form.field.value %}`
  (it posts `YYYY-MM-DD`).
- Employee: `forms.UUIDField()` + `{% employee_picker "employee_id" %}`; then `assert_manages` in the view.
- Money: `forms.DecimalField(max_digits=14, decimal_places=2)`; never float.
- Choices that match a DB CHECK: `forms.ChoiceField(choices=...)` with the same values.

## 9. File uploads

`{% file_upload "receipt_file_id" "expense_receipt" %}` uploads at once to the files app (`files:upload`, POST
multipart `file` + `purpose`, answer `{"file": {"id", "filename", "mime", "bytes", "purpose"}}`, max 4 MB, PDF/JPEG/
PNG/WebP) and puts the stored file id in the hidden input; your form then posts just the id and your service
attaches it (the TS `fileIds` flow). For a direct upload in your own form (CSV imports), use `<input type="file">`
with `hx-encoding="multipart/form-data"` and read `request.FILES`.

## 10. Errors and pages for special cases

- 404/403/500 templates exist; raise the typed errors and let the middleware answer.
- Static files are served by WhiteNoise (`hajiri.web.middleware.StaticFilesMiddleware`): from `static/` directly
  until `collectstatic` has filled `staticfiles/`, so no web server is needed in front. Reference them with
  `{% static %}`.
- `bare.html` for public pages (careers, kiosk). Public endpoints that are posted by other sites' browsers (none
  today) would need `@csrf_exempt`; device endpoints authenticate with their own key and are `@csrf_exempt` with
  a comment saying why.
- Bearer-token requests skip CSRF automatically.

## 11. Tests

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
- Run only your tests with your own database:
  `cd server && DJANGO_TEST_DATABASE=hajiri_django_test_<slice> .venv/bin/pytest -q tests/<app>`.

## 12. Worked example: a hub tab with a table, a modal form and a delete

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

## 13. Sign-in and sessions (owned by web)

`/login` (with the two-step step), `/login/2fa`, `/logout` (POST), `/change-password`, `/setup-two-step`,
`/me/profile/security` (sessions, recovery codes, turn off), `/_/web/me` (JSON). The session cookie
`hajiri_session` is HttpOnly, SameSite=Lax, 12 h, Secure when `COOKIE_SECURE=true`, shared with the TS API.
`/login` and `/login/2fa` also accept JSON (`{"email","password"}`) and answer like the TS API.
