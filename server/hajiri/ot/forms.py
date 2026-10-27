"""Django forms mirroring the zod schemas of apps/api/src/modules/ot/routes.ts (same limits and messages for the page)."""
from __future__ import annotations

from django import forms

from .services import CAP_DAY_MIN


class OtRequestForm(forms.Form):
    """POST /ot/requests: employeeId (optional, defaults to the signed-in employee), localDate, requestedMin >= 1, reason."""
    employee_id = forms.UUIDField(required=False, label='Employee')
    local_date = forms.DateField(label='Date')
    requested_min = forms.IntegerField(min_value=1, label='Minutes')
    reason = forms.CharField(max_length=500, required=False, label='Reason')

    def body(self) -> dict:
        d = self.cleaned_data
        b = {'localDate': d['local_date'].isoformat(), 'requestedMin': d['requested_min']}
        if d.get('employee_id'):
            b['employeeId'] = str(d['employee_id'])
        if d.get('reason'):
            b['reason'] = d['reason']
        return b


class ApproveSlipForm(forms.Form):
    """POST /ot/slips/:id/approve: approvedMin 0..240; an empty field is 0, as Number('') in the TS modal."""
    approved_min = forms.IntegerField(min_value=0, max_value=CAP_DAY_MIN, required=False, label='Approved minutes')

    def body(self) -> dict:
        # The TS modal always sent Number(approvedMin): an emptied field approves 0 minutes.
        v = self.cleaned_data.get('approved_min')
        return {'approvedMin': 0 if v is None else v}


class RejectSlipForm(forms.Form):
    """POST /ot/slips/:id/reject: reason 3..500 characters."""
    reason = forms.CharField(min_length=3, max_length=500, label='Reason', strip=False)

    def body(self) -> dict:
        return {'reason': self.cleaned_data['reason']}
