from django.conf import settings


def branding(request):
    user = getattr(request, "user", None)
    return {
        "APP_NAME": "Aqua AI Admin",
        "APP_TAGLINE": "AI-driven approval control plane",
        "SUPERADMIN_EMAILS": getattr(settings, "SUPERADMIN_EMAILS", []),
        "IS_SUPER_ADMIN": bool(getattr(user, "is_super_admin", False)) if user else False,
    }
