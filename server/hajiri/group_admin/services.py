"""
Group administration: the companies of a group, people's access to them, transfers between sister companies and
policy templates copied across companies. Business rules live in the modules below; this module is the public
surface other code imports.

    companies   list_companies, get_company, create_company, update_company, attachable_companies, attach_company
    access      list_access, unlinked_employees, grant_access, invite_user, revoke_access
    transfers   transfer_employee, list_transfers, target_companies, target_options, transfer_rules, set_transfer_rule
    policies    preview (preview_policies), apply (copy_policies), SECTIONS, SECTION_CHOICES

Group admins do everything; group HR (and group admins) transfer employees. A company id outside the actor's group
is 'company not found' (404), like any foreign id.
"""
from .access import grant_access, invite_user, list_access, revoke_access, unlinked_employees  # noqa: F401
from .common import GroupScope, can_administer, can_transfer, group_scope, logo_data_url  # noqa: F401
from .companies import (attach_company, attachable_companies, company_choices, create_company, get_company,  # noqa: F401
                        list_companies, update_company)
from .policies import SECTION_CHOICES, SECTION_KEYS, SECTIONS, SETTING_SECTIONS  # noqa: F401
from .policies import apply as copy_policies  # noqa: F401
from .policies import preview as preview_policies  # noqa: F401
from .transfers import (carries_leave, list_transfers, set_transfer_rule, target_companies, target_options,  # noqa: F401
                        transfer_employee, transfer_rules)
