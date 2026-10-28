"""
Page forms of the home slice (Home clock-in card, Settings tabs, Users & roles, Features).

A form reads the HTML post (checkboxes and switches, number boxes, repeated role inputs, empty selects) and builds the
TS-shaped body of the owning module's service (`company_settings.put_setting`, `core.users.admin_create_user`,
`punch.punch`, ...). The rules themselves (ranges, lengths, choices, password policy) are checked once by that service,
so a page post and a JSON call are refused with the same messages. A value a form cannot even read (text in a number
box, a reason that is too short) is raised as the same `ValidationError` issue shape.
"""
from __future__ import annotations

import uuid

from django import forms

from hajiri.core.errors import ValidationError

ON_VALUES = ('on', 'true', '1')


def form_issues(form: forms.Form, names: dict[str, str] | None = None) -> ValidationError:
    """ValidationError with the form's errors as TS issues; `names` maps form fields to body keys."""
    names = names or {}
    return ValidationError([{'path': names.get(f, f) if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


def form_issues_for(path: str, message: str) -> ValidationError:
    return ValidationError([{'path': path, 'message': message}])


class _Form(forms.Form):
    """Always bound to a post; raises instead of re-rendering (these pages show the error as a toast)."""
    paths: dict[str, str] = {}  # form field -> issue path, where the body key differs from the field name

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self, self.paths)
        return self.cleaned_data


# ---------------------------------------------------------------- fields

class RepeatedField(forms.Field):
    """Repeated inputs of one name (checkbox groups): the posted values in order, blanks dropped. Which values are
    allowed is the service's rule (roles, setting keys), so it reports unknown ones with its own message."""
    widget = forms.MultipleHiddenInput

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        return [v for v in (value or []) if v]

class SwitchField(forms.BooleanField):
    """A switch or checkbox: only 'on', 'true' or '1' count as on (Django's BooleanField also accepts any other
    non-empty text, which the page never sends)."""
    widget = forms.TextInput  # read the posted text as is; CheckboxInput would already turn any text into True

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        return str(value).lower() in ON_VALUES if value is not None else False


class SettingNumberField(forms.FloatField):
    """A Mantine NumberInput box: None when empty (so the caller can tell "left empty" apart), whole numbers as int,
    anything else as float. Text that is not a number is refused in the zod wording the setting schema uses."""
    default_error_messages = {'invalid': 'Expected number, received NaN'}  # hajiri.core.validation's wording

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        v = super().to_python(value.strip() if isinstance(value, str) else value)
        if v is None:
            return None
        return int(v) if float(v).is_integer() else v


# ---------------------------------------------------------------- Settings -> Company & rules

def _number_value(form: forms.Form, key: str, stored: dict):
    """The number to send for a box: the typed value; an emptied box for a stored value is 0 (Number('') in the TS
    page); an empty box for a value the setting does not have yet is left out (`...`), so the server's default
    applies."""
    v = form.cleaned_data.get(key)
    if v is None:
        return 0 if key in stored else ...
    return v


def generic_form_class(fields: list[dict]) -> type[forms.Form]:
    """A form for one GenericSettings tab (services.GENERIC): switches, number boxes, selects and text boxes."""
    attrs: dict = {}
    for f in fields:
        if f['type'] == 'boolean':
            attrs[f['key']] = SwitchField()
        elif f['type'] == 'number':
            attrs[f['key']] = SettingNumberField()
        else:
            attrs[f['key']] = forms.CharField(required=False, strip=False)
    return type('GenericSettingsForm', (_Form,), attrs)


def generic_body(fields: list[dict], stored: dict, post) -> dict:
    """GenericSettings: the stored value with the edited fields, leaving out '' and missing values."""
    form = generic_form_class(fields)(post)
    form.cleaned()
    p = dict(stored or {})
    for f in fields:
        k = f['key']
        # A value the stored setting does not have yet is only sent once its control is changed (the TS state `p`):
        # an untouched empty box or an unticked switch is left out, so the server applies its default.
        if f['type'] == 'boolean':
            on = form.cleaned_data[k]
            if on or k in p:
                p[k] = on
        elif f['type'] == 'number':
            v = _number_value(form, k, p)
            if v is not ...:
                p[k] = v
        else:
            p[k] = form.cleaned_data[k]
    return {k: v for k, v in p.items() if v != ''}


ATTENDANCE_NUMBERS = ('otMinThresholdMin', 'otRoundingMin', 'nightPremiumRate', 'autoClockOutAfterMin',
                      'regularisationBackdateDays', 'regularisationMonthlyCap', 'wfhPhotoRetentionDays')
# Switches and how the page shows them while the stored policy lacks them (the photo switch shows on).
ATTENDANCE_SWITCHES = (('requirePreapproval', False), ('nightWindowEnabled', False), ('wfhPhotoRequired', True))


class AttendanceRulesForm(_Form):
    otMinThresholdMin = SettingNumberField()
    otRoundingMin = SettingNumberField()
    nightPremiumRate = SettingNumberField()
    autoClockOutAfterMin = SettingNumberField()
    regularisationBackdateDays = SettingNumberField()
    regularisationMonthlyCap = SettingNumberField()
    wfhPhotoRetentionDays = SettingNumberField()
    requirePreapproval = SwitchField()
    nightWindowEnabled = SwitchField()
    wfhPhotoRequired = SwitchField()
    holidayWork = forms.CharField(required=False)
    latePenalty_enabled = SwitchField()
    latePenalty_lateCount = SettingNumberField()
    latePenalty_deductionDays = SettingNumberField()
    paths = {f'latePenalty_{k}': f'latePenalty.{k}' for k in ('enabled', 'lateCount', 'deductionDays')}

    def __init__(self, data, **kw):
        # The page names the penalty boxes latePenalty.enabled / .lateCount / .deductionDays.
        d = data.copy() if hasattr(data, 'copy') else dict(data)
        for k in ('enabled', 'lateCount', 'deductionDays'):
            if f'latePenalty.{k}' in data:
                d[f'latePenalty_{k}'] = data.get(f'latePenalty.{k}')
        super().__init__(d, **kw)

    def body(self, stored: dict) -> dict:
        """AttendanceRules state `p`: the stored policy with every control's value."""
        d = self.cleaned()
        sent = self.data
        p = dict(stored or {})
        for k in ATTENDANCE_NUMBERS:
            # Every box is posted by the page; a field left out keeps its stored value. An empty box for a value the
            # stored policy does not have is left out too, so the server applies its default, as the TS state did.
            if k in sent:
                v = _number_value(self, k, p)
                if v is not ...:
                    p[k] = v
        # Switches: a value the stored policy lacks is sent only when it differs from what the page showed.
        for k, shown in ATTENDANCE_SWITCHES:
            if k in p or d[k] != shown:
                p[k] = d[k]
        hw = d['holidayWork']
        if hw and ('holidayWork' in p or hw != 'substitute_leave'):  # the select shows substitute_leave when unset
            p['holidayWork'] = hw
        pen = dict(p.get('latePenalty') or {'enabled': False, 'lateCount': 3, 'deductionDays': 0.5})
        pen['enabled'] = d['latePenalty_enabled']
        if pen['enabled']:  # disabled inputs are not posted: keep the stored numbers
            pen['lateCount'] = d['latePenalty_lateCount'] if d['latePenalty_lateCount'] is not None else 0
            pen['deductionDays'] = d['latePenalty_deductionDays'] if d['latePenalty_deductionDays'] is not None else 0
        p['latePenalty'] = pen
        return p


def attendance_body(stored: dict, post) -> dict:
    return AttendanceRulesForm(post).body(stored)


class SecurityForm(_Form):
    require2faRoles = RepeatedField()

    def roles(self) -> list[str]:
        return self.cleaned()['require2faRoles']


class CodeNameForm(_Form):
    """Settings -> Branches and units: a code (upper-cased) and a name."""
    kind = forms.CharField(required=False)
    code = forms.CharField(required=False)
    name = forms.CharField(required=False)

    def branch(self) -> dict:
        d = self.cleaned()
        return {'code': d['code'].upper(), 'nameEn': d['name']}

    def unit(self) -> dict:
        d = self.cleaned()
        return {'kind': d['kind'] or 'department', 'code': d['code'].upper(), 'nameEn': d['name']}


class FeaturesForm(_Form):
    on = RepeatedField()

    def switched_on(self) -> set[str]:
        return set(self.cleaned()['on'])


# ---------------------------------------------------------------- Settings -> Users & roles

class UserNewForm(_Form):
    """Add user. The rules (email, name, password policy, roles, employee of this company) are core.users'."""
    email = forms.CharField(required=False)
    name = forms.CharField(required=False, strip=False)
    password = forms.CharField(required=False, strip=False)
    roles = RepeatedField()
    employee_id = forms.CharField(required=False)

    def body(self) -> dict:
        d = self.cleaned()
        b = {'email': d['email'], 'name': d['name'], 'password': d['password'], 'roles': list(d['roles'])}
        if d['employee_id']:
            b['employeeId'] = d['employee_id']
        return b


class UserUpdateForm(_Form):
    """Roles (the role picker posts roles_sent with the ticked roles) or status (Disable / Activate)."""
    status = forms.CharField(required=False)
    roles_sent = forms.CharField(required=False)
    roles = RepeatedField()

    def body(self) -> dict:
        d = self.cleaned()
        b: dict = {}
        if 'status' in self.data:
            b['status'] = d['status']
        if d['roles_sent']:
            b['roles'] = list(d['roles'])
        return b


class ResetTwoFactorForm(_Form):
    reason = forms.CharField(required=False, min_length=3,
                             error_messages={'min_length': 'Too small: expected string to have >=3 characters'})

    def reason_text(self) -> str:
        r = self.cleaned()['reason']
        if not r:  # min_length is not checked on an empty value
            raise form_issues_for('reason', 'Too small: expected string to have >=3 characters')
        return r


# ---------------------------------------------------------------- Home: clock in / out

class PunchForm(_Form):
    """HomePage doPunch: direction, the browser's time, the idempotency key, the location when the browser gave one,
    a reason (asked for by some rules) and the work-from-home photo."""
    direction = forms.CharField(required=False)
    clientTime = forms.CharField(required=False)
    idempotency_key = forms.CharField(required=False)
    lat = forms.FloatField(required=False)
    lng = forms.FloatField(required=False)
    accuracy = forms.FloatField(required=False)
    reason = forms.CharField(required=False)
    selfie = forms.CharField(required=False, strip=False)

    def body(self, client_time_default: str) -> dict:
        # A location the browser could not give (or garbled) is left out, never an error: the rules decide.
        self.is_valid()  # errors are ignored on purpose: an unreadable value is simply left out
        d = self.cleaned_data
        body = {'direction': d.get('direction') or 'in', 'source': 'web',
                'clientTime': d.get('clientTime') or client_time_default,
                'idempotencyKey': d.get('idempotency_key') or str(uuid.uuid4())}
        lat, lng, acc = d.get('lat'), d.get('lng'), d.get('accuracy')
        if lat is not None and lng is not None:
            body.update(lat=lat, lng=lng)
            if acc is not None:
                body['accuracyM'] = round(acc)
        if d.get('reason'):
            body['reason'] = d['reason']
        if d.get('selfie'):
            body['selfie'] = d['selfie']
        return body
