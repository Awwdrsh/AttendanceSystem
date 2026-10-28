from django.urls import path

from . import views

urlpatterns = [
    # Insights (AREA.insights: HR and executives)
    path('insights', views.insights_page, name='insights'),
    path('_/insights/export/<str:dataset>', views.insights_export, name='export'),
    # Reports hub (HR): the catalogue and the report builder
    path('reports', views.reports_page, name='reports'),
    path('reports/builder', views.builder_page, name='builder'),
    path('_/insights/reports/<str:key>/csv', views.report_csv, name='report_csv'),
    path('_/insights/reports/<str:key>/view', views.report_view, name='report_view'),
    path('_/insights/reports/<str:key>/view.csv', views.report_view_csv, name='report_view_csv'),
    path('_/insights/builder/form', views.builder_form, name='builder_form'),
    path('_/insights/builder/run', views.builder_run, name='builder_run'),
    path('_/insights/builder/export', views.builder_export, name='builder_export'),
    path('_/insights/builder/csv', views.builder_csv, name='builder_csv'),
    path('_/insights/builder/save', views.builder_save, name='builder_save'),
    path('_/insights/builder/saved', views.builder_saved, name='builder_saved'),
    path('_/insights/builder/saved/<uuid:id>/delete', views.builder_saved_delete, name='builder_saved_delete'),
]

# JSON API for Bearer-token clients (TS: /api/v1/insights/...).
from . import api  # noqa: E402

urlpatterns += [
    path('api/v1/insights', api.InsightsView.as_view(), name='api_insights'),
    path('api/v1/insights/filters', api.InsightsFiltersView.as_view(), name='api_filters'),
    path('api/v1/insights/export/<str:dataset>', api.InsightsExportView.as_view(), name='api_export'),
]
