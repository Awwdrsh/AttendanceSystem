"""The web app's own routes: the OpenAPI schema and docs, and the group overview JSON. Every page address is the
React app (hajiri.web.views.spa, the catch-all at the end of config/urls.py)."""
from django.urls import path

from . import api_docs, group

urlpatterns = [
    path('api/v1/group/overview', group.api_group_overview, name='api_group_overview'),
    # OpenAPI schema and docs of the JSON API (admins).
    path('api/schema', api_docs.SchemaView.as_view(), name='api_schema'),
    path('api/docs', api_docs.DocsView.as_view(url_name='web:api_schema'), name='api_docs'),
]
