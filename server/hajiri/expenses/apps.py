from django.apps import AppConfig


class ExpensesConfig(AppConfig):
    name = 'hajiri.expenses'
    label = 'expenses'

    def ready(self):
        # Decision handlers and inbox summaries for expense_claim, travel_request and travel_settlement.
        from . import services
        services.register()
