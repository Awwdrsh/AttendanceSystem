from django.apps import AppConfig


class TimesheetsConfig(AppConfig):
    name = 'hajiri.timesheets'
    label = 'timesheets'

    def ready(self):
        # Registers the 'timesheet' decision handler and inbox summary with the workflow engine.
        from . import services  # noqa: F401
