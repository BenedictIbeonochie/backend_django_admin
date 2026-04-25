from ..models import AdminAuditLog


def record(actor, action: str, *, target_type: str = "", target_id: str = "", **details):
    AdminAuditLog.objects.create(
        actor=actor if (actor and actor.is_authenticated) else None,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else "",
        details=details,
    )
