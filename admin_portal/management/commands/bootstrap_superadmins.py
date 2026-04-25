import os
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand

from admin_portal.models import AdminUser


class Command(BaseCommand):
    help = (
        "Ensure the configured SUPERADMIN_EMAILS exist locally as platform super-admins. "
        "Initial passwords are taken from STEVEN_PASSWORD / BEN_PASSWORD env vars (or "
        "<EMAIL_LOCALPART>_PASSWORD); if absent, a random password is printed once."
    )

    def handle(self, *args, **options):
        for email in settings.SUPERADMIN_EMAILS:
            local = email.split("@")[0].upper()
            env_var = f"{local}_PASSWORD"
            password = os.getenv(env_var)
            generated = False
            if not password:
                password = secrets.token_urlsafe(16)
                generated = True

            user, created = AdminUser.objects.get_or_create(
                email=email.lower(),
                defaults={
                    "full_name": email.split("@")[0].title(),
                    "is_staff": True,
                    "is_superuser": True,
                    "is_platform_super_admin": True,
                },
            )
            user.is_active = True
            user.is_staff = True
            user.is_superuser = True
            user.is_platform_super_admin = True
            user.set_password(password)
            user.save()

            verb = "created" if created else "updated"
            msg = f"Super-admin {verb}: {email}"
            if generated:
                msg += f"\n   initial password (CHANGE IT after first login!): {password}"
            else:
                msg += f"  (password set from {env_var})"
            self.stdout.write(self.style.SUCCESS(msg))
