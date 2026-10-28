"""HTMX actions for the notification bell: mark one or all read. Each answers 204 with an HX-Trigger so the bell and
the inbox list refresh themselves; the web app's layout renders the bell and the inbox page."""
from django.http import HttpResponse
from django.views.decorators.http import require_POST

from hajiri.core.errors import HttpError
from hajiri.web.decorators import login_required
from hajiri.web.htmx import toast
from hajiri.web.middleware import error_message

from . import service

TRIGGER = 'notifications-changed'


def _changed() -> HttpResponse:
    return HttpResponse(status=204, headers={'HX-Trigger': TRIGGER})


@require_POST
@login_required
def mark_read(request, notification_id):
    service.mark_read(request.hajiri_user.id, str(notification_id), company_id=request.hajiri_user.company_id)
    return _changed()


@require_POST
@login_required
def mark_all_read(request):
    try:
        service.mark_all_read(request.hajiri_user.id, company_id=request.hajiri_user.company_id)
    except HttpError as exc:   # NotificationsPage.tsx titles this toast itself
        return toast(request, HttpResponse(status=exc.status), error_message(exc), 'err', 'Could not mark as read')
    return _changed()
