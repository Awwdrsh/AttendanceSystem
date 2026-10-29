from django.urls import path

from . import api

urlpatterns = [
    # JSON API for the React app and Bearer-token clients (the TS routes under /api/v1/documents, api.py). Links handed
    # out by the React app (api/documents.js fileUrl, letterUrl, policyUrl) open here too. Ids are any one path
    # segment: the services parse them, so a malformed id answers 400 'Invalid UUID' as zod did, not 404. The fixed
    # paths come before /documents/<doc_id>.
    path('api/v1/documents/categories', api.categories, name='api_categories'),
    path('api/v1/documents/categories/<str:category_id>', api.category, name='api_category'),
    path('api/v1/documents/mine', api.mine, name='api_mine'),
    path('api/v1/documents/queue', api.queue, name='api_queue'),
    path('api/v1/documents/employee/<str:employee_id>', api.employee, name='api_employee'),
    path('api/v1/documents/placeholders', api.placeholders, name='api_placeholders'),
    path('api/v1/documents/letter-settings', api.letter_settings, name='api_letter_settings'),
    path('api/v1/documents/templates', api.templates, name='api_templates'),
    path('api/v1/documents/templates/preview', api.template_preview, name='api_template_preview'),
    path('api/v1/documents/templates/<str:template_id>', api.template, name='api_template'),
    path('api/v1/documents/letters', api.letters, name='api_letters'),
    path('api/v1/documents/letters/<str:letter_id>/html', api.letter_html, name='api_letter_html'),
    path('api/v1/documents/letters/<str:letter_id>/void', api.void, name='api_void'),
    path('api/v1/documents/letter-requests', api.letter_requests, name='api_letter_requests'),
    path('api/v1/documents/letter-requests/<str:request_id>/cancel', api.letter_request_cancel,
         name='api_letter_request_cancel'),
    path('api/v1/documents/policies', api.policies, name='api_policies'),
    path('api/v1/documents/policies/<str:policy_id>', api.policy, name='api_policy'),
    path('api/v1/documents/policies/<str:policy_id>/file', api.policy_file, name='api_policy_file'),
    path('api/v1/documents/policies/<str:policy_id>/acknowledge', api.acknowledge, name='api_acknowledge'),
    path('api/v1/documents/policies/<str:policy_id>/acks', api.policy_acks, name='api_policy_acks'),
    path('api/v1/documents/policies/<str:policy_id>/remind', api.policy_remind, name='api_policy_remind'),
    path('api/v1/documents/<str:doc_id>', api.document, name='api_document'),
    path('api/v1/documents/<str:doc_id>/file', api.document_file, name='api_doc_file'),
    path('api/v1/documents/<str:doc_id>/verify', api.verify, name='api_verify'),
]
