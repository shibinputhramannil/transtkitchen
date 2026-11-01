"""
Helpers for reading settings from environment variables.

Invalid values raise ImproperlyConfigured instead of falling back silently,
so a typo cannot quietly disable a security setting.
"""

import os

from django.core.exceptions import ImproperlyConfigured

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def env_str(name, default=None, required=False):
    value = os.environ.get(name)
    if value and value.strip():
        return value
    if required:
        raise ImproperlyConfigured(f"{name} must be set.")
    return default


def env_bool(name, default=False):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ImproperlyConfigured(
        f"{name} must be one of: true, false, 1, 0, yes, no, on, off (got {value!r})."
    )


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be an integer (got {value!r}).") from None


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]
