"""
Django settings for Hajiri.

Settings come from the environment and server/.env (scripts/setup.sh writes it; a value already in the environment
wins). DATA_ENCRYPTION_KEY and PASSWORD_PEPPER must never change on an existing install: they decrypt stored PAN, bank
and ID numbers and verify existing password hashes. An install that ran the retired TypeScript stack keeps them in
apps/api/.env, which is no longer read: setup.sh copies them into server/.env (see README.md, "Moving from the
TypeScript stack").
"""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f'{name} is not set (server/.env or the environment)')
    return value


def env_bool(name: str, default: bool) -> bool:
    return env(name, 'true' if default else 'false').lower() == 'true'


def database_from_url(url: str) -> dict:
    u = urlparse(url)
    if u.scheme not in ('postgres', 'postgresql'):
        raise ImproperlyConfigured(f'DATABASE_URL must be a postgres:// URL, got {u.scheme}://')
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': u.path.lstrip('/'),
        'USER': unquote(u.username or ''),
        'PASSWORD': unquote(u.password or ''),
        'HOST': u.hostname or 'localhost',
        'PORT': str(u.port or 5432),
        'CONN_MAX_AGE': 60,
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {'options': '-c TimeZone=UTC'},
    }


# Django's own signing key. Falls back to the pepper so a single generated secret is enough on existing installs.
SECRET_KEY = env('DJANGO_SECRET_KEY', os.environ.get('PASSWORD_PEPPER') or 'dev-only-insecure-key')
DEBUG = env_bool('DJANGO_DEBUG', False)
ALLOWED_HOSTS = [h for h in env('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h]

# Field encryption and password hashing secrets (the ciphertext and hash formats are those the TypeScript API wrote).
# Checked when first used, not at import, so migrations and tests run before secrets are configured.
DATA_ENCRYPTION_KEY = os.environ.get('DATA_ENCRYPTION_KEY', '')
PASSWORD_PEPPER = os.environ.get('PASSWORD_PEPPER', '')
RUN_WORKER = env_bool('RUN_WORKER', True)
SMTP_URL = os.environ.get('SMTP_URL', '')
SMS_WEBHOOK_URL = os.environ.get('SMS_WEBHOOK_URL', '')
# Other origins (comma separated) allowed to call the API with credentials, as the TS CORS_ORIGINS setting did.
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '')
# Page form posts from those origins (and any listed here) pass Django's CSRF origin check.
CSRF_TRUSTED_ORIGINS = sorted({o.strip() for o in (CORS_ORIGINS + ',' + os.environ.get('CSRF_TRUSTED_ORIGINS', '')).split(',')
                               if o.strip()})
# Behind an HTTPS reverse proxy that sets X-Forwarded-Proto.
if env_bool('TRUST_X_FORWARDED_PROTO', False):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.postgres',
    'django.contrib.staticfiles',
    'rest_framework',
    'drf_spectacular',
    'django_filters',
    'django_tasks_db',
    'hajiri.db',
    'hajiri.web',
    'hajiri.files',
    'hajiri.notifications',
    'hajiri.features',
    'hajiri.company_settings',
    'hajiri.reports',
    'hajiri.attendance',
    'hajiri.worker',
    'hajiri.integrations',
    'hajiri.workflow',
    'hajiri.gateway',
    'hajiri.devices',
    'hajiri.dashboard',
    'hajiri.leave',
    'hajiri.people',
    'hajiri.employees',
    'hajiri.lifecycle',
    'hajiri.shifts',
    'hajiri.regularisation',
    'hajiri.documents',
    'hajiri.hiring',
    'hajiri.careers',
    'hajiri.tasks',
    'hajiri.timesheets',
    'hajiri.helpdesk',
    'hajiri.engage',
    'hajiri.expenses',
    'hajiri.performance',
    'hajiri.learning',
    'hajiri.insights',
    'hajiri.relations',
    'hajiri.ot',
    'hajiri.attendance_tools',
    'hajiri.punch',
    'hajiri.field',
    'hajiri.kiosk',
    'hajiri.payroll',
    'hajiri.home',
    'hajiri.group',
    'hajiri.group_admin',
    'demo',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'hajiri.web.middleware.SecurityHeadersMiddleware',
    # Serves /static/ and the React build itself (inside the security headers), so a single-machine install needs no
    # web server in front.
    'hajiri.web.middleware.StaticFilesMiddleware',
    'django.middleware.common.CommonMiddleware',
    # CORS for /api/ (CORS_ORIGINS), answered first so a preflight never reaches a view.
    'hajiri.web.middleware.ApiCorsMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'hajiri.web.middleware.AuthMiddleware',
    # The TS sameOriginGuard instead of Django's CSRF token check (the React app sends no token).
    'hajiri.web.middleware.SameOriginMiddleware',
    'hajiri.web.middleware.HttpErrorMiddleware',
]
CSRF_FAILURE_VIEW = 'hajiri.web.middleware.csrf_failure'
CSRF_COOKIE_SAMESITE = 'Lax'
X_FRAME_OPTIONS = 'DENY'

ROOT_URLCONF = env('DJANGO_ROOT_URLCONF', 'config.urls')

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            # Templates render the API's own HTML only (certificates, offer letters, review print-outs).
            'context_processors': [],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {'default': database_from_url(env('DATABASE_URL', 'postgres://localhost:5433/hajiri'))}
# The test runner drops and recreates this database; give each parallel pytest process its own name.
DATABASES['default']['TEST'] = {'NAME': env('DJANGO_TEST_DATABASE', 'hajiri_test')}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LANGUAGE_CODE = 'en'
# Stored timestamps are UTC; business dates are Nepal dates (UTC+5:45) and shown in Bikram Sambat.
TIME_ZONE = 'Asia/Kathmandu'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# /static/ holds only what installed apps ship (collectstatic); the web app's files are the React build below.
STATICFILES_DIRS: list = []
# Until collectstatic has run (development, tests), WhiteNoise reads the files through the finders.
WHITENOISE_USE_FINDERS = env_bool('WHITENOISE_USE_FINDERS', True)
WHITENOISE_MAX_AGE = 3600

# The web app: the React build (frontend/, `vite build` -> server/frontend_build/, committed to git). WhiteNoise serves
# the folder at the site root (/assets/*, sw.js, manifest.webmanifest, icons); hajiri.web.views.spa answers its
# index.html for every page address. Vite names the files under /assets/ by content hash, so they are cached for a
# year; index.html, sw.js and the manifest are revalidated on every load.
FRONTEND_BUILD_DIR = Path(env('FRONTEND_BUILD_DIR', str(BASE_DIR / 'frontend_build')))
WHITENOISE_ROOT = FRONTEND_BUILD_DIR
WHITENOISE_IMMUTABLE_FILE_TEST = r'^/assets/.+-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+$'


def _static_headers(headers, path, url):
    from hajiri.web.middleware import static_headers
    static_headers(headers, path, url)


WHITENOISE_ADD_HEADERS_FUNCTION = _static_headers

SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool('COOKIE_SECURE', False)
# HTTPS-only installs: redirect plain HTTP (the /health probe is exempt) and send HSTS. Off by default because most
# installs run on a LAN over plain HTTP, as the TS server did.
SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', False)
SECURE_REDIRECT_EXEMPT = [r'^health$']
SECURE_HSTS_SECONDS = int(env('SECURE_HSTS_SECONDS', '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', False)
SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', False)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

# ---------------------------------------------------------------- JSON API (Django REST framework)
# Every endpoint of the React app (session cookie) and of terminals, kiosk tablets and Bearer-token integrations,
# under /api/. See hajiri/core/api.py and docs/django-conventions.md.
REST_FRAMEWORK = {
    # ?format= is a report parameter in the TS API (csv, json), not DRF's renderer override.
    'URL_FORMAT_OVERRIDE': None,
    # The session cookie with the TS Origin check (the React app), or a Bearer token.
    'DEFAULT_AUTHENTICATION_CLASSES': ['hajiri.core.api.SameOriginAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['hajiri.core.api.IsSignedIn'],
    'DEFAULT_RENDERER_CLASSES': ['hajiri.core.api.JsonRenderer'],
    'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser', 'rest_framework.parsers.FormParser',
                               'rest_framework.parsers.MultiPartParser'],
    'DEFAULT_PAGINATION_CLASS': 'hajiri.core.api.Pagination',
    'PAGE_SIZE': 50,
    'DEFAULT_FILTER_BACKENDS': ['django_filters.rest_framework.DjangoFilterBackend'],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER': 'hajiri.core.api.exception_handler',
    # No django.contrib.auth: request.user is the hajiri AuthUser, or None when signed out.
    'UNAUTHENTICATED_USER': None,
    'UNAUTHENTICATED_TOKEN': None,
    # Timestamps reach the renderer as datetimes, which it writes like JSON.stringify (UTC, milliseconds, Z).
    'DATETIME_FORMAT': None,
    'DATE_FORMAT': 'iso-8601',
    'COERCE_DECIMAL_TO_STRING': True,
    'UNICODE_JSON': True,
}
SPECTACULAR_SETTINGS = {
    'TITLE': 'Hajiri API',
    'DESCRIPTION': 'Attendance, HR and payroll. JSON endpoints for terminals, kiosk tablets and integrations.',
    'VERSION': '1',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    # One schema name per shared choice set (the same values appear in several serializers).
    'ENUM_NAME_OVERRIDES': {
        'ApprovalStatusEnum': ['pending', 'approved', 'rejected', 'cancelled'],
        'TicketStatusEnum': ['open', 'in_progress', 'waiting', 'resolved', 'closed'],
        'RegularisationKindEnum': ['missed_punch', 'wfh', 'on_duty', 'half_day', 'shift_change'],
        'ExpenseItemKindEnum': ['amount', 'mileage'],
        'InOutEnum': ['in', 'out'],
        'ApproveRejectEnum': ['approve', 'reject'],
        'CompanyRoleEnum': ['admin', 'hr', 'manager', 'employee', 'payroll_admin', 'attendance_admin', 'recruiter',
                            'executive'],
        'TravelDestinationEnum': ['valley', 'city', 'rural', 'international'],
        'RoundingModeEnum': ['nearest', 'up', 'down'],
    },
}

# ---------------------------------------------------------------- background work (django.tasks)
# Tasks are rows in the database (django_tasks_db), so a single-machine install still needs only PostgreSQL.
# `manage.py run_worker` runs the scheduler and a task worker in one process; larger installs run
# `manage.py run_scheduler` once and `manage.py db_worker` as often as needed.
TASKS = {
    'default': {
        'BACKEND': env('TASKS_BACKEND', 'django_tasks_db.DatabaseBackend'),
        'QUEUES': ['default'],
    },
}

# ---------------------------------------------------------------- email (SMTP_URL, as the TS API)
from hajiri.core.mail import email_settings  # noqa: E402

globals().update(email_settings(SMTP_URL))

_LOG_LEVEL = {'trace': 'DEBUG', 'fatal': 'CRITICAL'}.get(env('LOG_LEVEL', 'info'), env('LOG_LEVEL', 'info').upper())
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': _LOG_LEVEL},
}
