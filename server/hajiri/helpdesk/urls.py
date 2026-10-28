from django.urls import path

from . import views

urlpatterns = [
    path('me/help', views.mine, name='mine'),
    path('helpdesk', views.queue, name='queue'),
    path('_/helpdesk/tickets/new', views.new_ticket, name='new'),
    path('_/helpdesk/tickets/<uuid:ticket_id>', views.ticket, name='ticket'),
    path('_/helpdesk/tickets/<uuid:ticket_id>/messages', views.reply, name='reply'),
    path('_/helpdesk/tickets/<uuid:ticket_id>/update', views.update, name='update'),
    path('_/helpdesk/tickets/<uuid:ticket_id>/reopen', views.reopen, name='reopen'),
    path('_/helpdesk/tickets/<uuid:ticket_id>/close', views.close, name='close'),
    path('_/helpdesk/tickets/<uuid:ticket_id>/rate', views.rate, name='rate'),
    path('_/helpdesk/tickets/<uuid:ticket_id>/files/<uuid:file_id>', views.ticket_file, name='file'),
    path('_/helpdesk/categories/new', views.category_new, name='category_new'),
    path('_/helpdesk/categories/<uuid:category_id>', views.category_edit, name='category_edit'),
    path('_/helpdesk/settings', views.save_settings, name='settings'),
]

# JSON API for Bearer-token clients (TS: /api/v1/helpdesk/...).
from rest_framework.routers import SimpleRouter  # noqa: E402

from . import api  # noqa: E402

router = SimpleRouter(trailing_slash=False)
router.register('api/v1/helpdesk/tickets', api.HelpdeskViewSet, basename='api-ticket')
router.register('api/v1/helpdesk/categories', api.CategoryViewSet, basename='api-category')
router.register('api/v1/helpdesk/stats', api.StatsViewSet, basename='api-stats')
urlpatterns += router.urls
