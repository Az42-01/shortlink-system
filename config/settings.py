"""
Django settings for the ShortLinkSystem project.

阶段一目标：核心链路
    1. 提交长链接 -> 数据库自增 ID -> Base62 生成短码
    2. 访问短码 -> 优先 Redis 命中 -> miss 时回源 SQLite 并写回 Redis -> 301 跳转
    3. 点击日志通过后台线程批量落库，不阻塞跳转响应

所有与外部依赖相关的参数都可以通过环境变量覆盖，方便后续切换 MySQL / 云 Redis。
"""

import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    """读取布尔型环境变量，例如 DJANGO_DEBUG=1 / true / on。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_list(name, default):
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# 基础配置
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-cu=$6vv!c+0jnvr^gx5ld8-(9^%^nisdf-q#&9o%@39s=6p2f3",
)

DEBUG = _env_bool("DJANGO_DEBUG", True)

# 线上请通过 DJANGO_ALLOWED_HOSTS="short.example.com" 覆盖
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost"])

CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])


# ---------------------------------------------------------------------------
# 应用与中间件
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 业务应用
    "shortener",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# 数据库：阶段一使用 SQLite，后续切 MySQL 只需替换 ENGINE/NAME/USER/PASSWORD/HOST
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # SQLite 并发写入容易 "database is locked"，放大超时让后台批量写日志更稳
        "OPTIONS": {"timeout": 20},
    }
}


# ---------------------------------------------------------------------------
# Redis（redis-py 直连，不依赖 django-redis）
# ---------------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = _env_int("REDIS_PORT", 6379)
REDIS_DB = _env_int("REDIS_DB", 0)
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "") or None
REDIS_SOCKET_TIMEOUT = float(os.getenv("REDIS_SOCKET_TIMEOUT", "0.5"))
REDIS_CONNECT_TIMEOUT = float(os.getenv("REDIS_CONNECT_TIMEOUT", "0.5"))

# 短码 -> 原始链接 的缓存过期时间（秒），默认 24 小时
SHORTLINK_CACHE_TTL = _env_int("SHORTLINK_CACHE_TTL", 60 * 60 * 24)

# 本地开发时短链接的展示前缀；部署后设置为 https://s.example.com
SHORTLINK_BASE_URL = os.getenv("SHORTLINK_BASE_URL", "").rstrip("/")

# 点击日志异步批处理参数（阶段一用进程内队列，后续可替换为 Celery / Kafka）
CLICK_LOG_ASYNC = _env_bool("CLICK_LOG_ASYNC", True)
CLICK_LOG_BATCH_SIZE = _env_int("CLICK_LOG_BATCH_SIZE", 200)
CLICK_LOG_FLUSH_INTERVAL = float(os.getenv("CLICK_LOG_FLUSH_INTERVAL", "1.0"))
CLICK_LOG_QUEUE_MAXSIZE = _env_int("CLICK_LOG_QUEUE_MAXSIZE", 10000)


# ---------------------------------------------------------------------------
# 密码校验 / 国际化 / 静态文件
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "shortener": {
            "handlers": ["console"],
            "level": os.getenv("SHORTENER_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
