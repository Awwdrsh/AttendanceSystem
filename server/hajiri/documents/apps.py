from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    name = 'hajiri.documents'
    label = 'documents'

    def ready(self):
        # Registers the 'letter_request' decision handler with the workflow engine (letters.ts registerHandler).
        from . import services  # noqa: F401
