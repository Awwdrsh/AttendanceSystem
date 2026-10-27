from django.apps import AppConfig


class LearningConfig(AppConfig):
    name = 'hajiri.learning'
    label = 'learning'

    def ready(self):
        # Registers the 'training_nomination' decision handler with the workflow engine.
        from . import services  # noqa: F401
