"""Single-password basic auth for the Flask app.

Week 0-2 is single-user. Real auth (multi-tenant + tokens) is Week 5+.
"""

from __future__ import annotations

import functools
from typing import Callable

from flask import jsonify, request

from ssfish.config import settings


def _expected_password() -> str:
    return settings.flask_password.get_secret_value()


def require_password(view: Callable):
    """Decorator: require X-Auth-Password header or ?password=... query param."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-Auth-Password") or request.args.get("password")
        if not provided or provided != _expected_password():
            return jsonify({"error": "unauthorized", "hint": "set X-Auth-Password header"}), 401
        return view(*args, **kwargs)

    return wrapper


__all__ = ["require_password"]
