"""The health check and JSON sign-in for Bearer-token integrations (the browser uses the web app's sign-in pages)."""
from django.urls import path

from . import auth_api, health

urlpatterns = [
    path('health', health.health, name='health'),
    path('api/v1/auth/login', auth_api.login, name='api_login'),
    path('api/v1/auth/login/2fa', auth_api.login_2fa, name='api_login_2fa'),
    path('api/v1/auth/logout', auth_api.logout, name='api_logout'),
    path('api/v1/auth/me', auth_api.me, name='api_me'),
    path('api/v1/auth/change-password', auth_api.change_password, name='api_change_password'),
]
