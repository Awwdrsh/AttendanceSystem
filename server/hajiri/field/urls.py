from django.urls import path

from . import api

urlpatterns = [
    # JSON (TS paths)
    path('api/v1/field/policy', api.policy, name='api_policy'),
    path('api/v1/field/me', api.me, name='api_me'),
    path('api/v1/field/breaks/start', api.breaks_start, name='api_breaks_start'),
    path('api/v1/field/breaks/end', api.breaks_end, name='api_breaks_end'),
    path('api/v1/field/breaks', api.breaks, name='api_breaks'),
    path('api/v1/field/visits/start', api.visits_start, name='api_visits_start'),
    path('api/v1/field/visits/<str:id>/end', api.visit_end, name='api_visit_end'),
    path('api/v1/field/visits/<str:id>/photo', api.visit_photo, name='api_visit_photo'),
    path('api/v1/field/visits', api.visits, name='api_visits'),
    path('api/v1/field/locations', api.locations, name='api_locations'),
    path('api/v1/field/locations/<str:id>', api.location, name='api_location'),
    path('api/v1/field/staff', api.staff, name='api_staff'),
    path('api/v1/field/staff/<str:employee_id>', api.staff_update, name='api_staff_update'),
    path('api/v1/field/staff/<str:employee_id>/kiosk-reset', api.staff_kiosk_reset, name='api_staff_kiosk_reset'),
    path('api/v1/field/kiosks', api.kiosks, name='api_kiosks'),
    path('api/v1/field/kiosks/<str:id>', api.kiosk, name='api_kiosk'),
    path('api/v1/field/kiosks/<str:id>/pairing-code', api.kiosk_pairing_code, name='api_kiosk_pairing_code'),
    path('api/v1/field/kiosks/<str:id>/revoke', api.kiosk_revoke, name='api_kiosk_revoke'),
    path('api/v1/field/kiosks/<str:id>/unlock', api.kiosk_unlock, name='api_kiosk_unlock'),
    path('api/v1/field/kiosk-pin', api.kiosk_pin, name='api_kiosk_pin'),
    path('api/v1/field/kiosk-qr', api.kiosk_qr, name='api_kiosk_qr'),
    path('api/v1/field/my-roster', api.my_roster, name='api_my_roster'),
    path('api/v1/field/swap-colleagues', api.swap_colleagues, name='api_swap_colleagues'),
    path('api/v1/field/swaps', api.swaps, name='api_swaps'),
    path('api/v1/field/swaps/<str:id>/accept', api.swap_accept, name='api_swap_accept'),
    path('api/v1/field/swaps/<str:id>/decline', api.swap_decline, name='api_swap_decline'),
    path('api/v1/field/swaps/<str:id>/cancel', api.swap_cancel, name='api_swap_cancel'),
]
