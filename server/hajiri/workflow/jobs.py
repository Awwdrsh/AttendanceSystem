"""Worker hooks: SLA reminders reach the same approvers as the inbox (line manager, heads, groups, delegates)."""
from hajiri.worker.registry import register_hook

from .service import approvers_for_step


def _approvers(company_id, employee_id, step):
    return approvers_for_step(company_id, employee_id, step)


register_hook('workflow.approvers_for_step', _approvers)
