from django.apps import AppConfig


class LifecycleConfig(AppConfig):
    name = 'hajiri.lifecycle'
    label = 'lifecycle'

    def ready(self):
        # Probation reviews and resignations are decided in the approval inbox.
        from .api import register
        register()
