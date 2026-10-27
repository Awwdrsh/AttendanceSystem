from django.apps import AppConfig


class RegularisationConfig(AppConfig):
    name = 'hajiri.regularisation'
    label = 'regularisation'

    def ready(self):
        # Registers the 'regularisation_request' decision handler with the workflow engine.
        from . import services  # noqa: F401
