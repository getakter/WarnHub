"""Makes the request user available to model saves (created_by/updated_by, versions, audit)."""
import contextvars
from contextlib import contextmanager

_current_user = contextvars.ContextVar("ewce_current_user", default=None)


def get_current_user():
    user = _current_user.get()
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    return None


@contextmanager
def acting_as(user):
    token = _current_user.set(user)
    try:
        yield
    finally:
        _current_user.reset(token)


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_user.set(getattr(request, "user", None))
        try:
            return self.get_response(request)
        finally:
            _current_user.reset(token)
