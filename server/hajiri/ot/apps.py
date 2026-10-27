from django.apps import AppConfig


class OtConfig(AppConfig):
    name = 'hajiri.ot'
    label = 'ot'

    def ready(self):
        # Registers the ot_request decision handler with the workflow engine.
        from . import services  # noqa: F401
