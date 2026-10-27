from django.apps import AppConfig


class PayrollConfig(AppConfig):
    name = 'hajiri.payroll'
    label = 'payroll'

    def ready(self):
        # Tax savings requests are approved through the workflow engine (tax-savings.ts registerHandler/registerSummary).
        from hajiri.workflow.service import register_handler, register_summariser

        from .tax_savings import apply_tax_declaration_decision, tax_declaration_summary
        register_handler('tax_declaration', apply_tax_declaration_decision)
        register_summariser('tax_declaration', tax_declaration_summary)
