"""Request contracts of group administration (camelCase keys, as every JSON body in Hajiri)."""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.auth import ROLES
from hajiri.core.validation import IsoDateField

CODE_RE = r'^[A-Za-z0-9][A-Za-z0-9_-]{1,19}$'
PAN_RE = r'^\d{9}$'
LOGO_RE = r'^data:image/(png|jpeg|svg\+xml);base64,[A-Za-z0-9+/=]+$'


def _opt_text(max_length: int) -> serializers.CharField:
    return serializers.CharField(max_length=max_length, required=False, allow_null=True, allow_blank=True)


def _opt_uuid() -> serializers.UUIDField:
    return serializers.UUIDField(required=False, allow_null=True)


class _Profile(serializers.Serializer):
    nameNe = _opt_text(120)
    pan = serializers.RegexField(PAN_RE, required=False, allow_null=True, allow_blank=True,
                                 error_messages={'invalid': 'PAN has 9 digits'})
    address = _opt_text(300)
    logoDataUrl = serializers.RegexField(LOGO_RE, max_length=200_000, required=False, allow_null=True,
                                         error_messages={'invalid': 'a PNG, JPEG or SVG data URL'})


class CompanyIn(_Profile):
    """POST /api/v1/group/companies: a new company of the group, seeded with the statutory defaults."""
    code = serializers.RegexField(CODE_RE, error_messages={'invalid': 'Use 2 to 20 letters, digits, - or _.'})
    nameEn = serializers.CharField(min_length=1, max_length=120)


class CompanyPatchIn(_Profile):
    """PATCH /api/v1/group/companies/<id>: names and profile; a field left out is kept, null clears it."""
    nameEn = serializers.CharField(min_length=1, max_length=120, required=False)


class AttachIn(serializers.Serializer):
    companyId = serializers.UUIDField()


class AccessIn(serializers.Serializer):
    """Give or change a person's access to one company of the group."""
    email = serializers.EmailField(max_length=200)
    companyId = serializers.UUIDField()
    roles = serializers.ListField(child=serializers.ChoiceField(ROLES), min_length=1)
    employeeId = _opt_uuid()


class InviteIn(AccessIn):
    """A new account whose home is a company of the group (temporary password, changed at first sign-in)."""
    name = serializers.CharField(min_length=1, max_length=120)
    password = serializers.CharField(max_length=256, trim_whitespace=False)


class TransferIn(serializers.Serializer):
    """Move an employee of the active company to a sister company from `effectiveDate` (their first day there)."""
    employeeId = serializers.UUIDField()
    toCompanyId = serializers.UUIDField()
    effectiveDate = IsoDateField()
    empCode = serializers.CharField(max_length=40, required=False, allow_blank=True)
    designation = _opt_text(120)
    branchId = _opt_uuid()
    orgUnitId = _opt_uuid()
    managerId = _opt_uuid()
    shiftId = _opt_uuid()
    carryLeave = serializers.BooleanField(default=False)
    carrySalary = serializers.BooleanField(default=False)
    note = _opt_text(500)


class TransferRuleIn(serializers.Serializer):
    companyId = serializers.UUIDField()
    carryLeaveBalance = serializers.BooleanField()


class PolicyCopyIn(serializers.Serializer):
    """Copy policy templates from one company of the group to others."""
    sourceCompanyId = serializers.UUIDField()
    targetCompanyIds = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=50)
    sections = serializers.ListField(child=serializers.CharField(max_length=60), min_length=1, max_length=30)
    onConflict = serializers.ChoiceField(('skip', 'overwrite'), default='skip')

    def validate_sections(self, value):
        from .policies import SECTION_KEYS
        bad = [s for s in value if s not in SECTION_KEYS]
        if bad:
            raise serializers.ValidationError(f'unknown section {bad[0]}')
        return list(dict.fromkeys(value))
