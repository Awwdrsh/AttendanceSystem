from django.urls import path

from . import api, views

urlpatterns = [
    # pages
    path('me/profile/documents', views.mine, name='mine'),
    path('employees/documents', views.admin, name='admin'),
    # employee page tab (EmployeeDocumentsTab.tsx)
    path('_/documents/employee/<uuid:employee_id>', views.employee_tab, name='employee_tab'),
    # employee documents
    path('_/documents/docs', views.docs_grid, name='docs'),
    path('_/documents/docs/upload', views.upload, name='upload'),
    path('_/documents/docs/<uuid:doc_id>/edit', views.doc_edit, name='doc_edit'),
    path('_/documents/docs/<uuid:doc_id>/verify', views.verify, name='verify'),
    path('_/documents/docs/<uuid:doc_id>/reject', views.reject, name='reject'),
    path('_/documents/docs/<uuid:doc_id>/delete', views.doc_delete, name='doc_delete'),
    path('_/documents/docs/<uuid:doc_id>/file', views.doc_file, name='doc_file'),
    path('_/documents/review', views.review, name='review'),
    # document types
    path('_/documents/categories', views.categories, name='categories'),
    path('_/documents/categories/new', views.category_new, name='category_new'),
    path('_/documents/categories/<uuid:category_id>/toggle', views.category_toggle, name='category_toggle'),
    # templates and letter settings
    path('_/documents/templates', views.templates, name='templates'),
    path('_/documents/templates/new', views.template_new, name='template_new'),
    path('_/documents/templates/preview', views.template_preview, name='template_preview'),
    path('_/documents/templates/<uuid:template_id>', views.template_edit, name='template_edit'),
    path('_/documents/letter-settings', views.letter_settings, name='letter_settings'),
    # letters
    path('_/documents/letters', views.letters, name='letters'),
    path('_/documents/letters/issue', views.issue, name='issue'),
    path('_/documents/letters/preview', views.issue_preview, name='issue_preview'),
    path('_/documents/letters/<uuid:letter_id>/html', views.letter_html, name='letter_html'),
    path('_/documents/letters/<uuid:letter_id>/void', views.void, name='void'),
    # letter requests
    path('_/documents/requests', views.my_requests, name='my_requests'),
    path('_/documents/requests/new', views.request_new, name='request_new'),
    path('_/documents/requests/<uuid:request_id>/cancel', views.request_cancel, name='request_cancel'),
    # policies
    path('_/documents/policies', views.policies, name='policies'),
    path('_/documents/policies/mine', views.my_policies, name='my_policies'),
    path('_/documents/policies/new', views.policy_new, name='policy_new'),
    path('_/documents/policies/<uuid:policy_id>/archive', views.policy_archive, name='policy_archive'),
    path('_/documents/policies/<uuid:policy_id>/acks', views.policy_acks, name='policy_acks'),
    path('_/documents/policies/<uuid:policy_id>/remind', views.policy_remind, name='policy_remind'),
    path('_/documents/policies/<uuid:policy_id>/file', views.policy_file, name='policy_file'),
    path('_/documents/policies/<uuid:policy_id>/acknowledge', views.acknowledge, name='acknowledge'),
    # JSON API for Bearer-token clients (the TS routes under /api/v1/documents, api.py). Links handed out by the TS
    # app (api/documents.ts fileUrl, letterUrl, policyUrl) open here too.
    path('api/v1/documents/categories', api.categories, name='api_categories'),
    path('api/v1/documents/categories/<uuid:category_id>', api.category, name='api_category'),
    path('api/v1/documents/mine', api.mine, name='api_mine'),
    path('api/v1/documents/queue', api.queue, name='api_queue'),
    path('api/v1/documents/employee/<uuid:employee_id>', api.employee, name='api_employee'),
    path('api/v1/documents/placeholders', api.placeholders, name='api_placeholders'),
    path('api/v1/documents/letter-settings', api.letter_settings, name='api_letter_settings'),
    path('api/v1/documents/templates', api.templates, name='api_templates'),
    path('api/v1/documents/templates/preview', api.template_preview, name='api_template_preview'),
    path('api/v1/documents/templates/<uuid:template_id>', api.template, name='api_template'),
    path('api/v1/documents/letters', api.letters, name='api_letters'),
    path('api/v1/documents/letters/<uuid:letter_id>/html', api.letter_html, name='api_letter_html'),
    path('api/v1/documents/letters/<uuid:letter_id>/void', api.void, name='api_void'),
    path('api/v1/documents/letter-requests', api.letter_requests, name='api_letter_requests'),
    path('api/v1/documents/letter-requests/<uuid:request_id>/cancel', api.letter_request_cancel,
         name='api_letter_request_cancel'),
    path('api/v1/documents/policies', api.policies, name='api_policies'),
    path('api/v1/documents/policies/<uuid:policy_id>', api.policy, name='api_policy'),
    path('api/v1/documents/policies/<uuid:policy_id>/file', api.policy_file, name='api_policy_file'),
    path('api/v1/documents/policies/<uuid:policy_id>/acknowledge', api.acknowledge, name='api_acknowledge'),
    path('api/v1/documents/policies/<uuid:policy_id>/acks', api.policy_acks, name='api_policy_acks'),
    path('api/v1/documents/policies/<uuid:policy_id>/remind', api.policy_remind, name='api_policy_remind'),
    path('api/v1/documents/<uuid:doc_id>', api.document, name='api_document'),
    path('api/v1/documents/<uuid:doc_id>/file', api.document_file, name='api_doc_file'),
    path('api/v1/documents/<uuid:doc_id>/verify', api.verify, name='api_verify'),
]
