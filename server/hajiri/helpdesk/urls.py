from django.urls import path

# JSON API (TS: /api/v1/helpdesk/...), called by the React app with the cookie session.
from . import api

_API = 'api/v1/helpdesk/'
urlpatterns = [
    path(f'{_API}settings', api.SettingsView.as_view(), name='api-settings'),
    path(f'{_API}categories', api.CategoriesView.as_view(), name='api-categories'),
    path(f'{_API}categories/<str:category_id>', api.CategoryView.as_view(), name='api-category'),
    path(f'{_API}agents', api.AgentsView.as_view(), name='api-agents'),
    path(f'{_API}stats', api.StatsView.as_view(), name='api-stats'),
    path(f'{_API}tickets', api.TicketsView.as_view(), name='api-tickets'),
    path(f'{_API}tickets/mine', api.MyTicketsView.as_view(), name='api-tickets-mine'),
    path(f'{_API}tickets/<str:ticket_id>', api.TicketView.as_view(), name='api-ticket'),
    path(f'{_API}tickets/<str:ticket_id>/messages', api.MessagesView.as_view(), name='api-ticket-messages'),
    path(f'{_API}tickets/<str:ticket_id>/reopen', api.ReopenView.as_view(), name='api-ticket-reopen'),
    path(f'{_API}tickets/<str:ticket_id>/close', api.CloseView.as_view(), name='api-ticket-close'),
    path(f'{_API}tickets/<str:ticket_id>/rate', api.RateView.as_view(), name='api-ticket-rate'),
    path(f'{_API}tickets/<str:ticket_id>/files/<str:file_id>', api.TicketFileView.as_view(), name='api-ticket-file'),
]
