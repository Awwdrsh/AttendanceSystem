from django.urls import path

from . import views

urlpatterns = [
    path('_/notifications/read-all', views.mark_all_read, name='read_all'),
    path('_/notifications/<uuid:notification_id>/read', views.mark_read, name='read'),
]
