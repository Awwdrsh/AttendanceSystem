"""
`{% load hj %}`: filters for dates, money and numbers, and the shared components (see docs/django-conventions.md).

Filters: bs_date, bs_date_long, bs_date_short, bs_datetime, bs_month, gregorian, dual_date, npt_time, relative_time,
npr, money, num, minutes_hm, pct, devanagari, fiscal_label, status_label, tone, truncate_text, initials, role_label,
json_attr, qs.

Components: page_header/endpage_header, section/endsection, card/endcard, chart_card/endchart_card, modal/endmodal, drawer/enddrawer,
status_badge, empty_state, stat_strip, data_table, report_table, field, form_field, form_errors, bs_date_input,
bs_month_input, employee_picker, file_upload, qr_code, camera_capture, approval_chain, confirm_attrs, icon,
column_chart, line_chart, donut, hbars, trend_bars, sparkline.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from django import template
from django.template.loader import render_to_string
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import SafeString, mark_safe

from engines.bs_calendar import BS_MONTH_NAMES_EN, MAX_YEAR, MIN_YEAR, is_projected_bs_year

from .. import charts as ch
from .. import format as fmt
from ..nav import ROLE_LABEL

register = template.Library()


# ---------------------------------------------------------------- filters

register.filter('bs_date', fmt.fmt_date)
register.filter('bs_date_long', fmt.fmt_date_long)
register.filter('bs_date_short', fmt.fmt_date_short)
register.filter('bs_datetime', fmt.fmt_datetime)
register.filter('gregorian', fmt.fmt_gregorian)
register.filter('npt_time', fmt.fmt_time)
register.filter('relative_time', fmt.relative_time)
register.filter('minutes_hm', fmt.minutes_to_hm)
register.filter('pct', fmt.pct)
register.filter('fiscal_label', fmt.fiscal_label)
register.filter('devanagari', fmt.devanagari)


@register.filter
def dual_date(value):
    """{{ d|dual_date }} -> "12 Bhadra 2083" with the Gregorian date muted after it."""
    dd = fmt.dual_date(value)
    if dd.bs_long == fmt.DASH:
        return fmt.DASH
    return format_html('{} <span class="hj-muted hj-small">{}</span>', dd.bs_long, dd.ad)


@register.filter
def bs_month(value, month=None):
    """{{ year|bs_month:month }} or {{ obj|bs_month }} for an object/dict with bs_year and bs_month -> "Ashwin 2083"."""
    if month is not None:
        return fmt.bs_month_label(value, month)
    if isinstance(value, dict):
        return fmt.bs_month_label(value.get('bs_year') or value.get('year'), value.get('bs_month') or value.get('month'))
    if isinstance(value, (tuple, list)):
        return fmt.bs_month_label(value[0], value[1])
    return fmt.bs_month_label(getattr(value, 'bs_year', None) or value.year, getattr(value, 'bs_month', None) or value.month)


@register.filter
def npr(value, decimals=2):
    """NPR 12,34,567.00 (lakh grouping); {{ v|npr:0 }} for whole rupees."""
    return fmt.fmt_money(value, decimals=int(decimals))


@register.filter
def money(value, decimals=2):
    """12,34,567.00 without the currency."""
    return fmt.fmt_money(value, currency=False, decimals=int(decimals))


@register.filter
def num(value):
    return fmt.DASH if value is None or value == '' else fmt.fmt_num(value)


@register.filter
def truncate_text(value, n=40):
    return fmt.truncate(value, int(n))


@register.filter
def initials(name):
    parts = [p for p in re.split(r'\s+', str(name or '?')) if p]
    return ''.join(p[0] for p in parts[:2]).upper() or '?'


@register.filter
def role_label(role):
    return ROLE_LABEL.get(role, str(role))


@register.filter
def json_attr(value):
    """Safe JSON for an HTML attribute (x-data, hx-vals): {{ obj|json_attr }} inside double quotes."""
    return conditional_escape(json.dumps(value, default=str))


@register.simple_tag(takes_context=True)
def qs(context, **changes):
    """{% qs page=2 %}: the current query string with some parameters changed (empty value removes)."""
    request = context['request']
    params = request.GET.copy()
    for k, v in changes.items():
        if v in (None, ''):
            params.pop(k, None)
        else:
            params[k] = v
    s = params.urlencode()
    return f'?{s}' if s else '?'


# ---------------------------------------------------------------- status badges (components/StatusBadge.tsx)

STATUS: dict[str, tuple[str, str]] = {
    'Present': ('ok', 'Present'), 'Working': ('ok', 'Clocked in'), 'Late': ('warn', 'Late'), 'Absent': ('bad', 'Absent'),
    'OnLeave': ('info', 'On leave'), 'HalfDayLeave': ('info', 'Half-day leave'), 'HalfDay': ('warn', 'Half day'),
    'Holiday': ('idle', 'Holiday'), 'WeeklyOff': ('idle', 'Weekly off'), 'Incomplete': ('warn', 'Missing punch'),
    'WFH': ('info', 'Work from home'), 'OffShift': ('idle', 'Off shift'), 'Pending': ('accent', 'Pending'),
    'pending': ('accent', 'Pending'), 'approved': ('ok', 'Approved'), 'rejected': ('bad', 'Rejected'),
    'cancelled': ('idle', 'Cancelled'), 'detected': ('accent', 'Needs review'), 'posted': ('ok', 'Posted'),
    'comp_off': ('info', 'Comp-off'), 'active': ('ok', 'Active'), 'disabled': ('bad', 'Disabled'),
    'exited': ('idle', 'Exited'), 'draft': ('idle', 'Draft'), 'preflight_failed': ('bad', 'Blocked'),
    'computed': ('accent', 'Calculated'), 'reviewed': ('info', 'Reviewed'), 'approved_run': ('ok', 'Approved'),
    'locked': ('ok', 'Locked'), 'disbursed': ('ok', 'Paid'), 'filed': ('ok', 'Filed'), 'published': ('ok', 'Published'),
    'submitted': ('accent', 'Submitted'), 'verified': ('ok', 'Verified'), 'online': ('ok', 'Online'),
    'offline': ('bad', 'Offline'), 'NotComputed': ('idle', 'Not calculated'),
}


def humanize(s: str) -> str:
    spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', s.replace('_', ' ')).lower()
    return spaced[:1].upper() + spaced[1:]


@register.filter
def status_label(status):
    return STATUS.get(status, (None, humanize(str(status))))[1] if status else fmt.DASH


@register.filter
def tone(status):
    return STATUS.get(status, ('idle', ''))[0] if status else 'idle'


def live_status(status, *, on_date, today, last_out, first_in):
    """Today's "Incomplete" just means the person has not clocked out yet."""
    if on_date == today and status == 'Incomplete' and first_in and not last_out:
        return 'Working'
    return status


def _pill(t: str, label: str) -> SafeString:
    return format_html('<span class="hj-badge hj-tone-{}"><span class="hj-dot" aria-hidden="true"></span>{}</span>', t, label)


def status_badge_html(status, late=None, remote=None, label=None) -> SafeString:
    if not status:
        return _pill('idle', fmt.DASH)
    t, lab = STATUS.get(status, ('idle', humanize(str(status))))
    parts = [_pill(t, label or lab)]
    if late and int(late) > 0:
        parts.append(_pill('warn', f'{int(late)} min late'))
    if remote and status != 'WFH':
        parts.append(_pill('info', 'Remote'))
    return mark_safe(f'<span class="hj-badges">{"".join(parts)}</span>') if len(parts) > 1 else parts[0]


@register.simple_tag
def status_badge(status, late=None, remote=None, label=None):
    """{% status_badge row.status late=row.late_minutes remote=row.remote %}; every status gets a dot and a word."""
    return status_badge_html(status, late, remote, label)


@register.simple_tag
def badge(text, tone='idle', dot=False):
    """{% badge "Verified" "ok" %}: a plain badge in one of the tones ok, warn, bad, info, idle, accent."""
    if dot:
        return _pill(tone, text)
    return format_html('<span class="hj-badge hj-tone-{}">{}</span>', tone, text)


@register.simple_tag
def icon(name, size=None, label=None, cls=''):
    """{% icon "calendar" %}: a Tabler icon; decorative unless a label is given."""
    style = format_html(' style="font-size:{}px"', size) if size else ''
    if label:
        return format_html('<i class="ti ti-{} {}"{} role="img" aria-label="{}"></i>', name, cls, style, label)
    return format_html('<i class="ti ti-{} {}"{} aria-hidden="true"></i>', name, cls, style)


# ---------------------------------------------------------------- block components

class _BlockNode(template.Node):
    def __init__(self, tpl, nodelist, args, kwargs, slots=()):
        self.tpl, self.nodelist, self.args, self.kwargs, self.slots = tpl, nodelist, args, kwargs, slots

    def render(self, context):
        ctx = {k: v.resolve(context) for k, v in self.kwargs.items()}
        for i, (name, v) in enumerate(zip(self.args[0], self.args[1])):
            ctx[name] = v.resolve(context)
        ctx['body'] = mark_safe(self.nodelist.render(context))
        ctx['request'] = context.get('request')
        ctx['embedded'] = context.get('embedded', False)
        ctx['csrf_token'] = context.get('csrf_token')
        return render_to_string(self.tpl, ctx)


def _block_tag(name: str, tpl: str, positional: tuple[str, ...]):
    def compile_fn(parser, token):
        bits = token.split_contents()[1:]
        args, kwargs = [], {}
        for b in bits:
            m = re.match(r'^(\w+)=(.+)$', b)
            if m:
                kwargs[m[1]] = parser.compile_filter(m[2])
            else:
                args.append(parser.compile_filter(b))
        if len(args) > len(positional):
            raise template.TemplateSyntaxError(f'{name} takes at most {len(positional)} positional arguments')
        nodelist = parser.parse((f'end{name}',))
        parser.delete_first_token()
        return _BlockNode(tpl, nodelist, (positional[:len(args)], args), kwargs)
    register.tag(name, compile_fn)


# {% page_header "Title" subtitle="..." back="/url" back_label="Employees" %}<actions>{% endpage_header %}
_block_tag('page_header', 'components/page_header.html', ('title',))
# {% section "Title" hint="..." %}...{% endsection %}  (SectionTitle with the content below it)
_block_tag('section', 'components/section.html', ('title',))
# {% card %}...{% endcard %} / {% card "Title" hint="..." padding="lg" %}
_block_tag('card', 'components/card.html', ('title',))
# {% modal "id" "Title" size="md" open=True %}...{% endmodal %}; open with data-modal-open="id"
_block_tag('modal', 'components/modal.html', ('id', 'title'))
# {% drawer "id" "Title" %}...{% enddrawer %}
_block_tag('drawer', 'components/drawer.html', ('id', 'title'))
# {% alert "warn" title="..." %}text{% endalert %}
_block_tag('alert', 'components/alert.html', ('tone',))
# {% chart_card "Headcount" subtitle="..." legend=legend csv_url="?export=csv" table=table empty="No data" %}chart{% endchart_card %}
# legend: [{'label', 'color'}]; table: {'columns': [str], 'rows': [[label, number|None, ...]]} (ChartCard.tsx)
_block_tag('chart_card', 'components/chart_card.html', ('title',))


# ---------------------------------------------------------------- simple components

@register.inclusion_tag('components/empty_state.html')
def empty_state(title='Nothing here yet.', hint=None, action_url=None, action_label=None, icon='inbox', compact=False,
                action_attrs=''):
    return {'title': title, 'hint': hint, 'action_url': action_url, 'action_label': action_label, 'icon': icon,
            'compact': compact, 'action_attrs': action_attrs}


@register.inclusion_tag('components/stat_strip.html')
def stat_strip(stats):
    """stats: [{'label', 'value', 'hint'?, 'tone'? ok|warn|bad, 'url'?}] in one panel divided by hairlines."""
    out = []
    for s in stats:
        v = s.get('value')
        out.append({**s, 'value': fmt.fmt_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v})
    return {'stats': out, 'cols': min(len(out), 6)}


@register.inclusion_tag('components/field.html')
def field(label, value=None, tip=None, mono=False):
    """Small "label: value" pair used in detail panels."""
    return {'label': label, 'value': fmt.DASH if value in (None, '') else value, 'tip': tip, 'mono': mono}


@register.inclusion_tag('components/data_table.html', takes_context=True)
def data_table(context, table):
    return {'table': table, 'request': context.get('request')}


def report_cell(col_type, v):
    """One report cell: dates in Bikram Sambat, yes/no as badges, numbers right-aligned (ReportTable.tsx reportCell)."""
    if v is None or v == '':
        return fmt.DASH
    if col_type == 'date':
        return fmt.fmt_date(v if isinstance(v, date) else str(v))
    if col_type == 'bool':
        return format_html('<span class="hj-badge hj-tone-{}">{}</span>', 'ok' if v else 'idle', 'Yes' if v else 'No')
    return str(v)


@register.inclusion_tag('components/report_table.html')
def report_table(columns, rows, empty_title='Nothing matches these filters.', max_height='520px'):
    """columns: [{'key', 'header', 'type': text|number|date|bool}], rows: [dict]."""
    cols = [{**c, 'align': 'right' if c.get('type') == 'number' else 'left'} for c in columns]
    body = [[{'text': report_cell(c.get('type'), r.get(c['key'])), 'align': c['align']} for c in cols] for r in rows]
    return {'columns': cols, 'rows': body, 'empty_title': empty_title, 'max_height': max_height}


@register.simple_tag
def confirm_attrs(message, title='Are you sure?', label='Confirm', tone='danger'):
    """
    Attributes for a styled confirmation before an HTMX request or a form submit (ConfirmDialog.tsx):
    <button hx-post="..." {% confirm_attrs "Delete this shift? Rosters using it stop working." label="Delete" %}>
    """
    return format_html('hx-confirm="{}" data-confirm-title="{}" data-confirm-label="{}" data-confirm-tone="{}"',
                       message, title, label, tone)


# ---------------------------------------------------------------- form fields

@register.inclusion_tag('components/form_field.html')
def form_field(bound_field, label=None, help=None, cls='', **attrs):
    """{% form_field form.email %}: label, input, description and error in the house style."""
    wname = bound_field.field.widget.__class__.__name__
    kind = ('checkbox' if wname == 'CheckboxInput' else 'textarea' if wname == 'Textarea'
            else 'choices' if wname in ('RadioSelect', 'CheckboxSelectMultiple') else 'select' if 'Select' in wname
            else 'file' if 'FileInput' in wname else 'input')
    widget_attrs = {'class': {'checkbox': 'hj-check', 'choices': 'hj-choices', 'select': f'hj-select {cls}'.strip()}.get(kind, f'hj-input {cls}'.strip()), **attrs}
    if bound_field.errors:
        widget_attrs['aria-invalid'] = 'true'
        widget_attrs['aria-describedby'] = f'{bound_field.auto_id}-error'
    return {'f': bound_field, 'label': label if label is not None else bound_field.label, 'help': help or bound_field.help_text,
            'widget': bound_field.as_widget(attrs=widget_attrs), 'kind': kind,
            'required': bound_field.field.required}


@register.inclusion_tag('components/form_errors.html')
def form_errors(form):
    """Errors that belong to the whole form (non_field_errors), as an alert."""
    return {'errors': form.non_field_errors() if form is not None else []}


@register.inclusion_tag('components/bs_date_input.html')
def bs_date_input(name, value=None, label=None, required=False, min=None, max=None, holidays=None, error=None,
                  placeholder='Pick a date', disabled=False, id=None, description=None, clearable=True):
    """
    Bikram Sambat date picker (BsDatePicker.tsx) that posts the ISO (AD) date in a hidden input `name`.
    holidays: {iso: name}. The grid itself is drawn client-side by the hjDate Alpine component from the
    calendar table that the page embeds once (components/bs_calendar_data.html via base.html).
    """
    iso = fmt.iso_of(value)
    opts = {'value': iso or '', 'min': fmt.iso_of(min) or '', 'max': fmt.iso_of(max) or '',
            'holidays': {fmt.iso_of(k): v for k, v in (holidays or {}).items()}, 'required': bool(required)}
    return {'opts': opts, 'name': name, 'value': iso or '', 'display': fmt.fmt_date(iso) if iso else '', 'label': label,
            'required': required, 'min': fmt.iso_of(min) or '', 'max': fmt.iso_of(max) or '', 'holidays': holidays or {},
            'error': error, 'placeholder': placeholder, 'disabled': disabled, 'id': id or f'f-{name}',
            'description': description, 'clearable': clearable and not required}


@register.inclusion_tag('components/bs_month_input.html')
def bs_month_input(name_year='bs_year', name_month='bs_month', year=None, month=None, label=None):
    """BS month picker for payroll and statutory periods: "Ashwin 2083"."""
    return {'name_year': name_year, 'name_month': name_month, 'year': int(year) if year else None,
            'month': int(month) if month else None, 'label': label,
            'months': list(enumerate(BS_MONTH_NAMES_EN, start=1)), 'years': list(range(MAX_YEAR, MIN_YEAR - 1, -1))}


@register.inclusion_tag('components/employee_picker.html', takes_context=True)
def employee_picker(context, name='employee_id', value=None, label='Employee', required=False, status='active', error=None,
                    placeholder='Search by name or code', id=None, selected_label=None):
    """
    Searchable employee selector (EmployeePicker.tsx): options read "Name (code)", searched over HTMX at
    /_/web/employees/search, scoped like the employee list (HR company-wide, managers their people, others themselves).
    """
    label_text = selected_label
    if value and not label_text:
        from hajiri.core.scoping import is_uuid
        from hajiri.db.models import Employee
        # Only the active company's employees: an id from another company of a group shows no name.
        request = context.get('request')
        user = getattr(request, 'hajiri_user', None)
        cid = getattr(user, 'company_id', None)
        r = (Employee.objects.for_company(cid).filter(pk=str(value)).values('name_en', 'emp_code').first()
             if cid and is_uuid(value) else None)
        label_text = f'{r["name_en"]} ({r["emp_code"]})' if r else ''
    return {'name': name, 'value': value or '', 'label': label, 'required': required, 'status': status,
            'error': error, 'placeholder': placeholder, 'id': id or f'f-{name}', 'selected_label': label_text or ''}


@register.inclusion_tag('components/file_upload.html')
def file_upload(name, purpose, label='Attach file', accept='application/pdf,image/jpeg,image/png,image/webp',
                value=None, filename=None, required=False):
    """
    Pick a PDF or image, upload it straight away (max 4 MB) and keep the stored file's id in hidden input `name`
    (FileUpload.tsx). Posts multipart `file` + `purpose` to the files app's `files:upload` URL, which answers
    {"file": {"id", "filename", "mime", "bytes", "purpose"}}.
    """
    from ..nav import url_of
    return {'name': name, 'purpose': purpose, 'label': label, 'accept': accept, 'value': value or '',
            'filename': filename or '', 'required': required, 'upload_url': url_of('files:upload') or '/_/files/'}


@register.simple_tag
def qr_code(value, size=200, label='QR code'):
    """A QR code drawn as SVG (QrCode.tsx): dark modules on a white quiet zone so phones read it in dark mode too."""
    import segno
    qr = segno.make(str(value), error='m', micro=False)
    n = qr.symbol_size(border=4)[0]
    path = []
    for r, rowbits in enumerate(qr.matrix):
        for c, bit in enumerate(rowbits):
            if bit:
                path.append(f'M{c + 4} {r + 4}h1v1h-1z')
    return format_html(
        '<svg class="hj-qr" width="{}" height="{}" viewBox="0 0 {} {}" role="img" aria-label="{}" shape-rendering="crispEdges">'
        '<rect width="{}" height="{}" fill="#fff"/><path d="{}" fill="#000"/></svg>',
        size, size, n, n, label, n, n, ''.join(path))


@register.inclusion_tag('components/camera_capture.html')
def camera_capture(name='photo', title='Take a photo', confirm_label='Use this photo', id='camera', footer=None, submit=False):
    """
    Selfie capture (CameraCapture.tsx) in a modal: front camera when allowed, phone camera via a file picker otherwise.
    The JPEG (max 480 px wide, under 280 KB) lands as a data URL in hidden input `name`, then the enclosing form is
    submitted (or the `hj:photo` event fires on the modal for custom handling).
    """
    return {'name': name, 'title': title, 'confirm_label': confirm_label, 'id': id, 'footer': footer, 'submit': submit}


@register.inclusion_tag('components/approval_chain.html')
def approval_chain(object_type, employee_id=None, amount=None, target_id=None):
    """
    "Who will approve this" (ApprovalChainPreview.tsx). Loads /_/workflow/preview?object_type=...&employee_id=...&amount=...
    over HTMX when the workflow app provides the `workflow:preview` URL; renders nothing otherwise.
    """
    from ..nav import url_of
    return {'url': url_of('workflow:preview'), 'object_type': object_type, 'employee_id': employee_id or '',
            'amount': amount or '', 'id': target_id or f'chain-{object_type}'}


# ---------------------------------------------------------------- charts

@register.inclusion_tag('components/charts/column.html')
def column_chart(labels, series, height=200, stacked=False, money=False, aria_label=''):
    return {'c': ch.column_chart(labels, series, height=height, stacked=stacked, money=money, aria_label=aria_label)}


@register.inclusion_tag('components/charts/line.html')
def line_chart(labels, series, height=200, suffix='', aria_label='', min_zero=True):
    return {'c': ch.line_chart(labels, series, height=height, suffix=suffix, aria_label=aria_label, min_zero=min_zero)}


@register.inclusion_tag('components/charts/donut.html')
def donut(data, size=160, center_label='', aria_label=''):
    return {'c': ch.donut(data, size=size, center_label=center_label, aria_label=aria_label)}


@register.inclusion_tag('components/charts/hbars.html')
def hbars(data, color=None, max_rows=10):
    return {'c': ch.hbars(data, color=color, max_rows=max_rows)}


@register.inclusion_tag('components/charts/trend_bars.html')
def trend_bars(points, label, max=100, suffix='%', height=160):
    return {'c': ch.trend_bars(points, label=label, max=max, suffix=suffix, height=height)}


@register.inclusion_tag('components/charts/sparkline.html')
def sparkline(values, width=120, height=28, color=None):
    return {'c': ch.sparkline(values, width=width, height=height), 'color': color or 'var(--hj-hydro)'}


@register.simple_tag
def projected_year(year) -> bool:
    return is_projected_bs_year(int(year))


@register.filter
def get_item(d, key):
    try:
        return d.get(key) if isinstance(d, dict) else d[key]
    except (KeyError, IndexError, TypeError):
        return None


@register.filter
def attr(obj, name):
    from ..tables import get_value
    return get_value(obj, name)


@register.filter
def to_json(value) -> Any:
    """JSON inside a <script type="application/json"> block (escapes </script>)."""
    s = json.dumps(value, default=str).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return mark_safe(s)
