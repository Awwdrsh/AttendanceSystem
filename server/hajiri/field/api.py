"""JSON endpoints of the field module under /api/v1/field (the TS paths): the phone app and Bearer clients. Breaks,
visits, kiosks and the kiosk PIN/badge answer 404 'feature off' when the company switched field work off (featureGate)."""
from __future__ import annotations

from functools import wraps

from hajiri.core.errors import FeatureOffError
from hajiri.core.http import api_view, json_response, read_json
from hajiri.punch.api import photo_response
from hajiri.web.csv import csv_response
from hajiri.web.views import client_ip

from . import services


def gated(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        from hajiri.features.service import feature_on
        u = request.hajiri_user
        if u and u.company_id and not feature_on(u.company_id, 'field'):
            raise FeatureOffError('field')
        return view(request, *args, **kwargs)
    return wrapped


def _report(out):
    if isinstance(out, dict) and 'filename' in out and 'columns' in out:
        return csv_response(f"{out['filename']}.csv", out['columns'], out['rows'])
    return out


@api_view(methods=('GET', 'PUT'))
def policy(request):
    if request.method == 'PUT':
        return services.put_policy(request.hajiri_user, read_json(request))
    return services.get_policy(request.hajiri_user)


@api_view()
def me(request):
    return services.me(request.hajiri_user)


@api_view(methods=('POST',))
@gated
def breaks_start(request):
    return json_response(services.start_break(request.hajiri_user, read_json(request)), status=201)


@api_view(methods=('POST',))
@gated
def breaks_end(request):
    return services.end_break(request.hajiri_user)


@api_view()
@gated
def breaks(request):
    return _report(services.list_breaks(request.hajiri_user, request.GET.dict()))


@api_view(methods=('POST',))
@gated
def visits_start(request):
    return json_response(services.start_visit(request.hajiri_user, read_json(request, 2_000_000), client_ip(request)), status=201)


@api_view(methods=('POST',))
@gated
def visit_end(request, id):
    return services.end_visit(request.hajiri_user, id, read_json(request))


@api_view()
@gated
def visits(request):
    return _report(services.list_visits(request.hajiri_user, request.GET.dict()))


@api_view()
@gated
def visit_photo(request, id):
    return photo_response(services.visit_photo(request.hajiri_user, id))


@api_view(methods=('GET', 'POST'))
def locations(request):
    if request.method == 'POST':
        return json_response(services.create_location(request.hajiri_user, read_json(request)), status=201)
    return services.locations(request.hajiri_user)


@api_view(methods=('PUT', 'DELETE'))
def location(request, id):
    if request.method == 'DELETE':
        return services.delete_location(request.hajiri_user, id)
    return services.update_location(request.hajiri_user, id, read_json(request))


@api_view()
def staff(request):
    return services.staff(request.hajiri_user)


@api_view(methods=('PUT',))
def staff_update(request, employee_id):
    return services.update_staff(request.hajiri_user, employee_id, read_json(request))


@api_view(methods=('POST',))
def staff_kiosk_reset(request, employee_id):
    return services.reset_kiosk_pin(request.hajiri_user, employee_id)


@api_view(methods=('GET', 'POST'))
@gated
def kiosks(request):
    if request.method == 'POST':
        return json_response(services.create_kiosk(request.hajiri_user, read_json(request)), status=201)
    return services.kiosks(request.hajiri_user)


@api_view(methods=('PUT',))
@gated
def kiosk(request, id):
    return services.update_kiosk(request.hajiri_user, id, read_json(request))


@api_view(methods=('POST',))
@gated
def kiosk_pairing_code(request, id):
    return services.kiosk_pairing_code(request.hajiri_user, id)


@api_view(methods=('POST',))
@gated
def kiosk_revoke(request, id):
    return services.revoke_kiosk(request.hajiri_user, id)


@api_view(methods=('POST',))
@gated
def kiosk_unlock(request, id):
    return services.unlock_kiosk(request.hajiri_user, id)


@api_view(methods=('PUT',))
@gated
def kiosk_pin(request):
    return services.set_kiosk_pin(request.hajiri_user, read_json(request))


@api_view()
@gated
def kiosk_qr(request):
    res = json_response(services.kiosk_qr(request.hajiri_user))
    res['Cache-Control'] = 'no-store'
    return res


@api_view()
def my_roster(request):
    return services.my_roster(request.hajiri_user, request.GET.dict())


@api_view()
def swap_colleagues(request):
    return services.swap_colleagues(request.hajiri_user, request.GET.dict())


@api_view(methods=('GET', 'POST'))
def swaps(request):
    if request.method == 'POST':
        return json_response(services.request_swap(request.hajiri_user, read_json(request)), status=201)
    return services.swaps(request.hajiri_user, request.GET.dict())


@api_view(methods=('POST',))
def swap_accept(request, id):
    return services.accept_swap(request.hajiri_user, id, read_json(request))


@api_view(methods=('POST',))
def swap_decline(request, id):
    return services.decline_swap(request.hajiri_user, id, read_json(request))


@api_view(methods=('POST',))
def swap_cancel(request, id):
    return services.cancel_swap(request.hajiri_user, id)
