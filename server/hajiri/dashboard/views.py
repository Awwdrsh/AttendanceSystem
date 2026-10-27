"""Overview (DashboardPage.tsx): headline numbers, arrivals, attendance trend, needs-attention list, payroll."""
from django.shortcuts import render
from django.views.decorators.http import require_GET

from hajiri.web.decorators import require_module

from . import services


@require_GET
@require_module('insights')
def overview(request):
    return render(request, 'dashboard/overview.html', services.overview(request.hajiri_user))
