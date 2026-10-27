from django.apps import AppConfig


class LeaveConfig(AppConfig):
    name = 'hajiri.leave'
    label = 'leave'

    def ready(self):
        # Registers the leave_request decision handler with the workflow engine.
        from . import services  # noqa: F401
