"""
Who may administer the group, and the companies they may reach.

Group administration reads and writes across the companies of ONE group: the active company's group
(`AuthUser.group_id`). A company id from outside that group behaves as not found, exactly like a foreign record id.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from hajiri.core.auth import AuthUser
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.scoping import is_uuid
from hajiri.db.models import Company, GroupMember

Role = GroupMember.Role


@dataclass(frozen=True)
class GroupScope:
    """The actor's group and its companies (by id), loaded once per call."""
    group_id: str
    group_name: str
    companies: dict[str, Company]

    @property
    def ids(self) -> list[str]:
        return list(self.companies)

    def company(self, company_id, message: str = 'company not found') -> Company:
        """A company of the group; any other id (another group, a standalone company, junk) is not found."""
        c = self.companies.get(str(company_id)) if is_uuid(company_id) else None
        if c is None:
            raise NotFoundError(message)
        return c

    def sorted(self) -> list[Company]:
        from hajiri.core.collation import collate_key
        return sorted(self.companies.values(), key=lambda c: collate_key(c.name_en))


def can_administer(user: AuthUser) -> bool:
    """Companies, access and policy templates of the group: group admins."""
    return bool(user.group_id) and Role.GROUP_ADMIN in user.group_roles


def can_transfer(user: AuthUser) -> bool:
    """Transfers between sister companies: group HR and group admins."""
    return bool(user.group_id) and user.has_group_role(Role.GROUP_HR)


def group_scope(user: AuthUser, *, hr: bool = False) -> GroupScope:
    """The actor's group, for a group admin (hr=True: group HR too). ForbiddenError otherwise."""
    if user.must_change_password:
        raise ForbiddenError('password change required')
    allowed = can_transfer(user) if hr else can_administer(user)
    if not allowed:
        raise ForbiddenError('requires role group_hr' if hr else 'requires role group_admin')
    companies = {str(c.id): c for c in Company.objects.in_group(user.group_id).select_related('group')}
    name = next((c.group.name_en for c in companies.values()), '')
    return GroupScope(str(user.group_id), name, companies)


# ---------------------------------------------------------------- logos

LOGO_MAX_PX = 320
LOGO_MAX_CHARS = 200_000          # the company.profile schema's limit on logoDataUrl
LOGO_MAX_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 40_000_000


def logo_data_url(data: bytes) -> str:
    """An uploaded logo as the data URL company.profile stores: at most 320 px on its longer side, PNG (keeps
    transparency), EXIF orientation applied."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(data) > LOGO_MAX_BYTES:
        raise BadRequestError('The logo is too large; use an image under 4 MB.')
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_PIXELS:
                raise BadRequestError('The logo is too large; use an image under 4 MB.')
            img = ImageOps.exif_transpose(img)
            img = img.convert('RGBA' if img.mode in ('RGBA', 'LA', 'P') else 'RGB')
            img.thumbnail((LOGO_MAX_PX, LOGO_MAX_PX), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='PNG', optimize=True)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise BadRequestError('That file is not an image we can read.') from None
    url = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    if len(url) > LOGO_MAX_CHARS:
        raise BadRequestError('The logo is too detailed; use a simpler or smaller image.')
    return url
