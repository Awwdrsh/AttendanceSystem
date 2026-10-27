from django.apps import AppConfig


class FieldConfig(AppConfig):
    name = 'hajiri.field'
    label = 'field'

    def ready(self):
        # The 'shift_swap' decision handler and inbox summary (swaps.ts registerHandler / registerSummariser).
        from hajiri.workflow.service import register_handler, register_summariser

        from . import services
        register_handler('shift_swap', services.apply_swap_decision)
        register_summariser('shift_swap', services.swap_summary)
