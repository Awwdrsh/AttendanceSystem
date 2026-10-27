from django.urls import path

from . import views

urlpatterns = [
    # Pages
    path('', views.index, name='index'),
    path('me/notifications', views.notifications, name='notifications'),
    path('settings', views.settings_page, name='settings'),
    path('settings/users', views.users_page, name='users'),
    path('settings/features', views.features_page, name='features'),
    path('settings/setup', views.setup_page, name='setup'),
    # Home fragments
    path('_/home/today', views.today_fragment, name='today'),
    path('_/home/punch', views.punch, name='punch'),
    path('_/home/requests', views.requests_fragment, name='requests'),
    path('_/home/month', views.month_fragment, name='month'),
    # Settings fragments
    path('_/home/settings/attendance', views.settings_attendance, name='settings_attendance'),
    path('_/home/settings/attendance/preview', views.settings_preview, name='settings_preview'),
    path('_/home/settings/security', views.settings_security, name='settings_security'),
    path('_/home/settings/branches', views.settings_branch, name='settings_branch'),
    path('_/home/settings/units', views.settings_unit, name='settings_unit'),
    path('_/home/settings/<str:tab>', views.settings_generic, name='settings_generic'),
    path('_/home/users/new', views.user_new, name='user_new'),
    path('_/home/users/<uuid:id>', views.user_update, name='user_update'),
    path('_/home/users/<uuid:id>/reset', views.user_reset, name='user_reset'),
    path('_/home/features', views.features_save, name='features_save'),
    path('_/home/setup/<str:action>', views.setup_action, name='setup_action'),
]
