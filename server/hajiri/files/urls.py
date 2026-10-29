from django.urls import path

from . import api

urlpatterns = [
    # TS /api/v1/files (React app, Bearer clients). Ids are <str:...> so a malformed one is the TS 400 "Invalid UUID".
    path('api/v1/files', api.upload, name='api_upload'),
    path('api/v1/files/<str:file_id>', api.serve, name='api_serve'),
]
