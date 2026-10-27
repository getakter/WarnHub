"""Django settings for WarnHub (early warning collection). Internal package/env prefix: ewce / EWCE_."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "apps.core",
    "apps.plugins",
    "apps.sources",
    "apps.collection",
    "apps.mailbox",
    "apps.ui",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.CurrentUserMiddleware",
]

ROOT_URLCONF = "ewce.urls"
WSGI_APPLICATION = "ewce.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.ui.context_processors.ewce",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": env("DB_ENGINE", "ewce.db.mariadb_compat"),
        "NAME": env("DB_NAME", "ewce"),
        "USER": env("DB_USER", "root"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "127.0.0.1"),
        "PORT": env("DB_PORT", "3306"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {
            "charset": "utf8mb4",
            "collation": "utf8mb4_unicode_ci",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
        "TEST": {
            "NAME": env("DB_TEST_NAME", "test_ewce"),
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_unicode_ci",
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "ui:dashboard"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-us"
# Stored in UTC (USE_TZ); displayed in Bangladesh time.
TIME_ZONE = "Asia/Dhaka"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
DATA_DIR = Path(env("EWCE_DATA_DIR", str(BASE_DIR / "data")))
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "media/"
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# --- Product specific (env prefix EWCE_ is internal and stays stable across renames) ----
EWCE_PRODUCT_NAME = env("EWCE_PRODUCT_NAME", "WarnHub")
EWCE_PRODUCT_TAGLINE = env("EWCE_PRODUCT_TAGLINE", "Early warning collection")
EWCE_ARTIFACT_ROOT = DATA_DIR / "artifacts"
EWCE_USER_AGENT = env(
    "EWCE_USER_AGENT", f"{EWCE_PRODUCT_NAME}-Collector/1.0 (early warning collection; contact: ops@example.org)"
)
EWCE_MAX_BODY_CHARS = int(env("EWCE_MAX_BODY_CHARS", "200000"))
EWCE_ENABLE_OCR = env_bool("EWCE_ENABLE_OCR", False)
EWCE_TESSERACT_CMD = env("EWCE_TESSERACT_CMD", "")
EWCE_OCR_LANG = env("EWCE_OCR_LANG", "ben+eng")
EWCE_ENABLE_PLAYWRIGHT = env_bool("EWCE_ENABLE_PLAYWRIGHT", False)
EWCE_PLUGIN_MODULES = env_list("EWCE_PLUGIN_MODULES", "")
EWCE_CUSTOM_PLUGIN_DIRS = [BASE_DIR / "custom_plugins"]
EWCE_WORKER_THREADS = int(env("EWCE_WORKER_THREADS", "3"))
EWCE_USE_SYSTEM_CERTS = env_bool("EWCE_USE_SYSTEM_CERTS", True)
EWCE_MIN_PACKET_BYTES = 16 * 1024 * 1024

LOG_DIR = Path(env("EWCE_LOG_DIR", str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "structured"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "ewce.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "structured",
            "delay": True,
        },
    },
    "loggers": {
        "ewce": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "django": {"handlers": ["console", "file"], "level": "WARNING"},
    },
}
