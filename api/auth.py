"""Single-password basic auth for the Flask app.

Week 0-2 is single-user. Real auth (multi-tenant + tokens) is Week 5+.

SECURITY (retrospective finding): the prior version also accepted the password
via `?password=...` query parameter. That was a high-severity security gap
because query params leak into browser history, reverse-proxy access logs,
Referer headers, and shell history. Query-param mode is now removed. Only
the `X-Auth-Password` header is accepted.
"""

from __future__ import annotations

import functools
from typing import Callable

from flask import jsonify, request

# Import the MODULE not the singleton, so tests can swap
# `ssflow.config.settings` via `monkeypatch` and we'll read the fresh
# value on each request.
from ssflow import config as _config


def _expected_password() -> str:
    return _config.settings.flask_password.get_secret_value()


def require_password(view: Callable):
    """Decorator: require the X-Auth-Password header to match settings.flask_password."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-Auth-Password")
        if not provided or provided != _expected_password():
            return jsonify(
                {
                    "error": "unauthorized",
                    "hint": "pass the X-Auth-Password header (query-param auth is no longer supported)",
                }
            ), 401
        return view(*args, **kwargs)

    return wrapper


__all__ = ["require_password"]
