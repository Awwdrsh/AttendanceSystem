from django.urls import path
from rest_framework.routers import SimpleRouter

from . import api

urlpatterns = []

router = SimpleRouter(trailing_slash=False)
router.register('api/v1/notifications', api.NotificationViewSet, basename='api-notification')
urlpatterns += router.urls
