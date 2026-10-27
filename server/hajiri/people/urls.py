from django.urls import path

from . import views

urlpatterns = [
    # People hub (everyone) and Profile hub.
    path('people', views.directory, name='directory'),
    path('people/org-chart', views.org_chart, name='org_chart'),
    path('me/profile', views.my_profile, name='profile'),
    path('me/profile/privacy', views.my_data, name='privacy'),
    # Employees hub tabs owned here (HR).
    path('employees/structure', views.structure, name='structure'),
    path('employees/requests', views.change_requests, name='requests'),
    # Fragments.
    path('_/people/<uuid:id>/card', views.card, name='card'),
    path('_/people/<uuid:id>/avatar', views.avatar, name='avatar'),
    path('_/people/<uuid:id>/avatar/change', views.employee_avatar, name='avatar_change'),
    path('_/people/<uuid:id>/avatar/editor', views.avatar_editor, name='avatar_editor'),
    path('_/people/me/avatar', views.my_avatar, name='my_avatar'),
    path('_/people/<uuid:id>/sections', views.sections, name='sections'),
    path('_/people/<uuid:id>/sections/<str:section>', views.section_edit, name='section_edit'),
    path('_/people/<uuid:id>/history', views.history, name='history'),
    path('_/people/<uuid:id>/career', views.career, name='career'),
    path('_/people/<uuid:id>/career/new', views.career_new, name='career_new'),
    path('_/people/<uuid:id>/career/<uuid:event_id>/cancel', views.career_cancel, name='career_cancel'),
    path('_/people/me/requests', views.my_requests, name='my_requests'),
    path('_/people/me/requests/<uuid:id>/cancel', views.request_cancel, name='request_cancel'),
    path('_/people/requests/<uuid:id>/reveal', views.request_reveal, name='request_reveal'),
    path('_/people/requests/<uuid:id>/proof', views.request_proof, name='request_proof'),
    path('_/people/requests/<uuid:id>/approve', views.request_approve, name='request_approve'),
    path('_/people/requests/<uuid:id>/reject', views.request_reject, name='request_reject'),
    path('_/people/structure/<str:kind>/new', views.structure_edit, name='structure_new'),
    path('_/people/structure/<str:kind>/<uuid:id>/edit', views.structure_edit, name='structure_edit'),
    path('_/people/structure/<str:kind>/<uuid:id>/delete', views.structure_delete, name='structure_delete'),
    path('_/people/policy', views.policy, name='policy'),
]
