"""Daily task reminders (module-jobs.ts `taskReminders`): due today, and once when a task has just become overdue."""
from hajiri.worker.registry import daily_job

from .services import task_reminders


@daily_job('taskReminders')
def task_reminders_job(now):
    return task_reminders(now)
