from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden


def super_admin_required(view_func):
    """Allow only Steven@humara.io / Ben@humara.io (platform super-admins)."""

    @login_required(login_url="admin_portal:login")
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if not getattr(user, "is_super_admin", False):
            return HttpResponseForbidden(
                "Only Steven@humara.io and Ben@humara.io can perform this action."
            )
        return view_func(request, *args, **kwargs)

    return _wrapped


def admin_required(view_func):
    """Any active admin in this control plane."""

    @login_required(login_url="admin_portal:login")
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_active:
            return HttpResponseForbidden("Your admin account is inactive.")
        return view_func(request, *args, **kwargs)

    return _wrapped
