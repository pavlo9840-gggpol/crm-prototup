import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from platformdirs import user_data_path

BASE_DIR = Path(__file__).resolve().parent.parent
AUTOZAPAZ_DATA_DIR = Path(
    os.environ.get(
        "AUTOZAPAZ_DATA_DIR",
        user_data_path("AutoZapaz", appauthor=False),
    )
).expanduser()
AUTOZAPAZ_DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG=0.")
    SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"

_allowed_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "")
if DEBUG:
    ALLOWED_HOSTS = [host.strip() for host in _allowed_hosts.split(",") if host.strip()] or ["localhost", "127.0.0.1", "[::1]"]
else:
    ALLOWED_HOSTS = [host.strip() for host in _allowed_hosts.split(",") if host.strip()]
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must contain the production hostnames.")
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "core.tenant_context.InstallationSetupMiddleware",
    "core.tenant_context.CompanyContextMiddleware",
    "core.tenant_context.LicenseRequiredMiddleware",
    "core.tenant_context.LicenseFeatureMiddleware",
    "core.tenant_context.WorkstationLimitMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "core.context.layout",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": AUTOZAPAZ_DATA_DIR / "db.sqlite3"}}
LANGUAGE_CODE = "uk"
TIME_ZONE = "Europe/Kyiv"
USE_I18N = True
USE_TZ = True
DATE_FORMAT = "d.m.Y"
DATETIME_FORMAT = "d.m.Y H:i"
SHORT_DATE_FORMAT = "d.m.Y"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_URL = "/media/"
MEDIA_ROOT = AUTOZAPAZ_DATA_DIR / "media"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTOZAPAZ_LICENSE_PUBLIC_KEY = os.environ.get("AUTOZAPAZ_LICENSE_PUBLIC_KEY", "")
LICENSE_ENFORCEMENT_ENABLED = os.environ.get(
    "AUTOZAPAZ_REQUIRE_LICENSE", "0" if DEBUG else "1"
) == "1"

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True

# The bundled local Waitress server uses HTTP on loopback. Enable secure
# cookies only when an HTTPS reverse proxy/terminator is configured.
SECURE_LOCAL_COOKIES = os.environ.get("AUTOZAPAZ_SECURE_COOKIES", "0") == "1"
SESSION_COOKIE_SECURE = SECURE_LOCAL_COOKIES
CSRF_COOKIE_SECURE = SECURE_LOCAL_COOKIES
