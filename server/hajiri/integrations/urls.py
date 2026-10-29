"""The health check and the /api/v1/auth endpoints of the TS API (sign-in, two-step sign-in, sessions and account
administration) for the React app and Bearer-token integrations."""
from django.urls import path

from . import auth_api, health

urlpatterns = [
    path('health', health.health, name='health'),
    path('api/v1/auth/login', auth_api.login, name='api_login'),
    path('api/v1/auth/login/2fa', auth_api.login_2fa, name='api_login_2fa'),
    path('api/v1/auth/logout', auth_api.logout, name='api_logout'),
    path('api/v1/auth/me', auth_api.me, name='api_me'),
    path('api/v1/auth/change-password', auth_api.change_password, name='api_change_password'),
    path('api/v1/auth/2fa', auth_api.two_factor, name='api_2fa'),
    path('api/v1/auth/2fa/setup', auth_api.two_factor_setup, name='api_2fa_setup'),
    path('api/v1/auth/2fa/enable', auth_api.two_factor_enable, name='api_2fa_enable'),
    path('api/v1/auth/2fa/recovery-codes', auth_api.two_factor_recovery_codes, name='api_2fa_recovery_codes'),
    path('api/v1/auth/2fa/disable', auth_api.two_factor_disable, name='api_2fa_disable'),
    path('api/v1/auth/sessions', auth_api.sessions, name='api_sessions'),
    path('api/v1/auth/sessions/revoke-others', auth_api.sessions_revoke_others, name='api_sessions_revoke_others'),
    path('api/v1/auth/sessions/<str:session_id>', auth_api.session_revoke, name='api_session_revoke'),
    path('api/v1/auth/users', auth_api.user_list, name='api_users'),
    path('api/v1/auth/users/<str:user_id>', auth_api.user_detail, name='api_user'),
    path('api/v1/auth/users/<str:user_id>/2fa/reset', auth_api.user_two_factor_reset, name='api_user_2fa_reset'),
]
