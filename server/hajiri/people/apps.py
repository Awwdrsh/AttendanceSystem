from django.apps import AppConfig


class PeopleConfig(AppConfig):
    name = 'hajiri.people'
    label = 'people'

    def ready(self):
        # Profile change requests are decided in the approval inbox: wire the handler and the inbox summary.
        from .change_requests import register
        register()
