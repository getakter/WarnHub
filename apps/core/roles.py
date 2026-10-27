"""Two roles, implemented as Django auth groups.

Admin    - configure everything (sources, feeds, rules, mailboxes, import/export, rollback).
Operator - view, run feeds, upload/paste, mark messages needs_attention.
Superusers are always Admins.
"""
from functools import wraps

from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied

ADMIN_GROUP = "WarnHub Admin"
OPERATOR_GROUP = "WarnHub Operator"


def _in_group(user, name):
    return user.groups.filter(name=name).exists()


def is_admin(user):
    return bool(user and user.is_authenticated and (user.is_superuser or _in_group(user, ADMIN_GROUP)))


def is_operator(user):
    return bool(user and user.is_authenticated and (is_admin(user) or _in_group(user, OPERATOR_GROUP)))


def ensure_groups():
    from django.contrib.auth.models import Group, Permission

    admin, _ = Group.objects.get_or_create(name=ADMIN_GROUP)
    operator, _ = Group.objects.get_or_create(name=OPERATOR_GROUP)
    ewce_perms = Permission.objects.filter(
        content_type__app_label__in=["core", "sources", "collection", "mailbox"]
    )
    admin.permissions.set(ewce_perms)
    operator.permissions.set(ewce_perms.filter(codename__startswith="view_"))
    return admin, operator


def _guard(check):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            if not check(request.user):
                raise PermissionDenied("Your role does not allow this action.")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


admin_required = _guard(is_admin)
operator_required = _guard(is_operator)


class OperatorRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not is_operator(request.user):
            raise PermissionDenied("Your role does not allow this action.")
        return super().dispatch(request, *args, **kwargs)


class AdminRequiredMixin(AccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not is_admin(request.user):
            raise PermissionDenied("Only Admins can change configuration.")
        return super().dispatch(request, *args, **kwargs)
