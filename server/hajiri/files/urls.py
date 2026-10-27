from django.urls import path

from . import views

urlpatterns = [
    path('_/files/', views.upload, name='upload'),
    path('_/files/<uuid:file_id>', views.serve, name='serve'),
]
