"""The kiosk page (/kiosk, KioskPage.tsx): a full-screen clock-in station for a shared tablet. It has no session; its
script (static kiosk/kiosk.js) talks to the JSON endpoints under /api/v1/kiosk with the kiosk key it got when paired."""
from __future__ import annotations

from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
def kiosk_page(request):
    return render(request, 'kiosk/page.html')
