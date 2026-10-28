from django.apps import AppConfig


class GroupAdminConfig(AppConfig):
    """Administration of a group of companies: companies of the group, group roles, access of people across
    companies. Rules live in hajiri.core.companies."""
    name = 'hajiri.group_admin'
    label = 'group_admin'
