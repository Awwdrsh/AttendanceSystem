from django.urls import path
from rest_framework.routers import SimpleRouter

from . import api, views

urlpatterns = [
    path('_/notifications/read-all', views.mark_all_read, name='read_all'),
    path('_/notifications/<uuid:notification_id>/read', views.mark_read, name='read'),
]

router = SimpleRouter(trailing_slash=False)
router.register('api/v1/notifications', api.NotificationViewSet, basename='api-notification')
urlpatterns += router.urls
