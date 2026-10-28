"""
Page forms of the field module. A form reads the HTML side (checkboxes, number inputs, empty selects, repeated
location pickers, the browser's position) and gives the service its TS-shaped body; the rules live once in
serializers.py and are checked by the service, so a page post and a JSON call are refused with the same messages.
"""
from __future__ import annotations

from django import forms

from hajiri.attendance_tools.forms import FilterForm, PageForm, RepeatedField

from .serializers import BREAK_KINDS


def _round(v: float) -> int:
    return round(v)


class GeoForm(PageForm):
    """The browser's position (hidden lat, lng, accuracy inputs); left out when the browser gave none."""
    lat = forms.CharField(required=False)
    lng = forms.CharField(required=False)
    accuracy = forms.CharField(required=False)

    def geo(self) -> dict:
        d = self.cleaned()
        try:
            if d['lat'] and d['lng']:
                out = {'lat': float(d['lat']), 'lng': float(d['lng'])}
                if d['accuracy']:
                    out['accuracyM'] = _round(float(d['accuracy']))
                return out
        except ValueError:
            pass
        return {}


class VisitStartForm(GeoForm):
    place = forms.CharField(required=False)
    purpose = forms.CharField(required=False)
    photo = forms.CharField(required=False, strip=False)

    def body(self) -> dict:
        d = self.cleaned()
        return {'place': d['place'], **({'purpose': d['purpose']} if d['purpose'] else {}),
                **({'photo': d['photo']} if d['photo'] else {}), **self.geo()}


class VisitEndForm(GeoForm):
    outcome = forms.CharField(required=False)

    def body(self) -> dict:
        d = self.cleaned()
        return {**({'outcome': d['outcome']} if d['outcome'] else {}), **self.geo()}


class KioskForm(PageForm):
    name = forms.CharField(required=False)
    branch_id = forms.CharField(required=False)
    location_id = forms.CharField(required=False)
    selfie_required = forms.BooleanField(required=False)

    def body(self) -> dict:
        d = self.cleaned()
        return {'name': d['name'], 'branchId': d['branch_id'] or None, 'locationId': d['location_id'] or None,
                'selfieRequired': d['selfie_required']}


class LocationForm(PageForm):
    name = forms.CharField(required=False)
    address = forms.CharField(required=False)
    branch_id = forms.CharField(required=False)
    lat = forms.FloatField(required=False)
    lng = forms.FloatField(required=False)
    radius_m = forms.FloatField(required=False)
    active = forms.BooleanField(required=False)
    body_names = {'radius_m': 'radiusM'}

    def body(self) -> dict:
        d = self.cleaned()
        return {'name': d['name'], 'address': d['address'] or None, 'branchId': d['branch_id'] or None, 'lat': d['lat'],
                'lng': d['lng'], 'radiusM': int(d['radius_m']) if d['radius_m'] else 150, 'active': d['active']}


class FieldRulesForm(PageForm):
    """Clock-in location mode, the break kinds and the field visit rules."""
    geofenceMode = forms.CharField(required=False)
    fieldVisitClockIn = forms.BooleanField(required=False)
    fieldPhotoRequired = forms.BooleanField(required=False)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        for k in BREAK_KINDS:
            self.fields[f'{k}_enabled'] = forms.BooleanField(required=False)
            self.fields[f'{k}_paid'] = forms.BooleanField(required=False)
            self.fields[f'{k}_maxMin'] = forms.FloatField(required=False)

    def policy(self) -> dict:
        d = self.cleaned()
        return {'geofenceMode': d['geofenceMode'] or 'flag',
                'breaks': {k: {'enabled': d[f'{k}_enabled'], 'paid': d[f'{k}_paid'], 'maxMin': int(d[f'{k}_maxMin'] or 0) or 5}
                           for k in BREAK_KINDS},
                'fieldVisitClockIn': d['fieldVisitClockIn'], 'fieldPhotoRequired': d['fieldPhotoRequired']}


class StaffForm(PageForm):
    """One switch or picker of the staff table: which='field' (field_work) or which='locations' (location_ids)."""
    which = forms.CharField(required=False)
    field_work = forms.BooleanField(required=False)
    location_ids = RepeatedField(required=False)

    def __init__(self, data=None, **kw):
        self.sent = set(data.keys()) if data is not None else set()
        if data is not None:
            data = {'which': data.get('which'), 'field_work': data.get('field_work'), 'location_ids': data.getlist('location_ids')}
        super().__init__(data, **kw)

    def body(self) -> dict:
        d = self.cleaned()
        body = {}
        if 'field_work' in self.sent or d['which'] == 'field':
            body['fieldWork'] = d['field_work']
        if d['which'] == 'locations':
            body['locationIds'] = d['location_ids']
        return body


class SwapForm(PageForm):
    colleague_id = forms.CharField(required=False)
    date = forms.CharField(required=False)
    reason = forms.CharField(required=False)

    def body(self) -> dict:
        d = self.cleaned()
        return {'colleagueId': d['colleague_id'], 'localDate': d['date'], **({'reason': d['reason']} if d['reason'] else {})}


class KioskPinForm(PageForm):
    pin = forms.CharField(required=False, strip=False)
    again = forms.CharField(required=False, strip=False)

    def clean(self):
        d = super().clean()
        if d.get('pin', '') != d.get('again', ''):
            raise forms.ValidationError('The PINs do not match')
        return d


class VisitsFilterForm(FilterForm):
    """/attendance/visits: ?view=visits|breaks&date=&span=day|week&over=1"""
    view = forms.ChoiceField(choices=[('visits', 'visits'), ('breaks', 'breaks')], required=False, initial='visits')
    date = forms.DateField(required=False, input_formats=['%Y-%m-%d'])
    span = forms.ChoiceField(choices=[('day', 'day'), ('week', 'week')], required=False, initial='day')
    over = forms.BooleanField(required=False)


class DateFilterForm(FilterForm):
    date = forms.DateField(required=False, input_formats=['%Y-%m-%d'])
