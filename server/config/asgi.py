"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_asgi_application()

# RUN_WORKER=true runs the background worker inside the web process, as the TS server did.
from hajiri.worker.runner import maybe_start_in_process  # noqa: E402

maybe_start_in_process()
