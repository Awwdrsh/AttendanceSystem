from django.apps import AppConfig


class GroupConfig(AppConfig):
    """Group-wide views over the companies of a group (overview, combined reports). Reads across companies only
    through hajiri.core.companies.group_company_ids(user) and Model.objects.for_companies(ids)."""
    name = 'hajiri.group'
    label = 'group'
