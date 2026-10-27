"""Worker jobs and hooks of the people app (module-jobs.ts careerEventsApplied; integrations.ts hire hook)."""
from hajiri.worker.registry import daily_job, register_hook

from .career import apply_due_career_events, record_career_event


@daily_job('careerEventsApplied')
def career_events_applied(now):
    return apply_due_career_events(now)


def _record(*, company_id, employee_id, kind, effective_date, details=None, note=None, actor_id=None):
    return record_career_event(company_id=company_id, employee_id=employee_id, kind=kind, effective_date=effective_date,
                               details=details or {}, note=note, actor_id=actor_id)


register_hook('people.record_career_event', _record)
