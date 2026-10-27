from django.apps import AppConfig


class HiringConfig(AppConfig):
    name = 'hajiri.hiring'
    label = 'hiring'

    def ready(self):
        # Registers the job_requisition and job_offer decision handlers and inbox summaries with the workflow engine.
        from . import services  # noqa: F401
