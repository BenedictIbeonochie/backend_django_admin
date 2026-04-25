"""The orchestration that connects new profiles → GPT-4 → actions → alerts."""
import logging
from datetime import datetime, timezone as dt_tz
from typing import Iterable, List

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import (
    AdminAuditLog,
    AIAccountReview,
    AIFlag,
    ExternalBreederProfile,
    ExternalConsultantProfile,
    ExternalUser,
)
from . import openai_client, slack_client, email_client

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- helpers

def _serialize_user(user: ExternalUser) -> dict:
    if not user:
        return {}
    return {
        "id": str(user.id),
        "email": user.email,
        "username": user.username,
        "name": user.name or f"{user.first_name} {user.last_name}".strip(),
        "phone": user.phone,
        "role": user.role,
        "is_verified": user.is_verified,
        "created_at": user.date_joined.isoformat() if user.date_joined else None,
    }


def _serialize_consultant(p: ExternalConsultantProfile) -> dict:
    return {
        "subject_type": "consultant",
        "id": str(p.id),
        "company_name": p.company_name,
        "bio": p.bio,
        "website": p.website,
        "business_phone": p.business_phone,
        "business_address": p.business_address,
        "verification_level": p.verification_level,
        "is_verified": p.is_verified,
        "is_active": p.is_active,
        "admin_status": p.admin_status,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "metadata": p.metadata,
        "user": _serialize_user(p.user),
    }


def _serialize_breeder(p: ExternalBreederProfile) -> dict:
    return {
        "subject_type": "breeder",
        "id": str(p.id),
        "company_name": p.company_name,
        "bio": p.bio,
        "website": p.website,
        "business_phone": p.business_phone,
        "business_address": p.business_address,
        "verification_level": p.verification_level,
        "is_verified": p.is_verified,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "metadata": p.metadata,
        "user": _serialize_user(p.user),
    }


# --------------------------------------------------------------------------- actions

def _apply_actions_to_consultant(consultant: ExternalConsultantProfile, decision: str) -> List[dict]:
    """Apply the AI's decision to the live consultant profile in the main DB."""
    applied: List[dict] = []
    now = timezone.now()
    metadata = dict(consultant.metadata or {})
    metadata.setdefault("ai_history", []).append(
        {"at": now.isoformat(), "decision": decision, "actor": "ai-control-plane"}
    )
    if decision == "approved":
        consultant.admin_status = "approved"
        consultant.is_active = True
        consultant.is_verified = True
        consultant.verified_at = now
        consultant.admin_notes = "Auto-approved by Aqua AI control plane."
        applied.append({"action": "approve", "target": "consultant", "id": str(consultant.id)})
    elif decision == "rejected":
        consultant.admin_status = "rejected"
        consultant.is_active = False
        consultant.is_verified = False
        consultant.admin_notes = "Auto-rejected by Aqua AI control plane."
        applied.append({"action": "reject", "target": "consultant", "id": str(consultant.id)})
    else:  # flagged
        consultant.admin_status = "needs_info"
        consultant.is_active = False
        consultant.admin_notes = "Held for super-admin review by Aqua AI control plane."
        applied.append({"action": "hold_for_admin", "target": "consultant", "id": str(consultant.id)})
    consultant.metadata = metadata
    consultant.updated_at = now
    consultant.save(
        update_fields=[
            "admin_status",
            "is_active",
            "is_verified",
            "verified_at",
            "admin_notes",
            "metadata",
            "updated_at",
        ]
    )
    return applied


def _apply_actions_to_breeder(breeder: ExternalBreederProfile, decision: str) -> List[dict]:
    applied: List[dict] = []
    now = timezone.now()
    metadata = dict(breeder.metadata or {})
    metadata.setdefault("ai_history", []).append(
        {"at": now.isoformat(), "decision": decision, "actor": "ai-control-plane"}
    )
    if decision == "approved":
        breeder.is_active = True
        breeder.is_verified = True
        breeder.verified_at = now
        applied.append({"action": "approve", "target": "breeder", "id": str(breeder.id)})
    elif decision == "rejected":
        breeder.is_active = False
        breeder.is_verified = False
        applied.append({"action": "reject", "target": "breeder", "id": str(breeder.id)})
    else:
        breeder.is_active = False
        applied.append({"action": "hold_for_admin", "target": "breeder", "id": str(breeder.id)})
    breeder.metadata = metadata
    breeder.updated_at = now
    breeder.save(
        update_fields=[
            "is_active",
            "is_verified",
            "verified_at",
            "metadata",
            "updated_at",
        ]
    )
    return applied


# --------------------------------------------------------------------------- decision policy

def _decide(verdict: dict) -> str:
    raw = (verdict.get("decision") or "").lower()
    confidence = float(verdict.get("confidence") or 0)
    if raw == "approved" and confidence >= settings.AI_APPROVE_THRESHOLD:
        return "approved"
    if raw == "rejected" and confidence <= (1 - settings.AI_REJECT_THRESHOLD):
        # explicit rejection at any reasonable confidence still rejects
        return "rejected"
    if raw == "rejected":
        return "rejected"
    return "flagged"


# --------------------------------------------------------------------------- public api

@transaction.atomic
def review_subject(profile) -> AIAccountReview:
    """Run the AI review on a single ExternalConsultantProfile or ExternalBreederProfile."""
    if isinstance(profile, ExternalConsultantProfile):
        payload = _serialize_consultant(profile)
        subject_type = "consultant"
    elif isinstance(profile, ExternalBreederProfile):
        payload = _serialize_breeder(profile)
        subject_type = "breeder"
    else:
        raise TypeError(f"Unsupported profile type: {type(profile)!r}")

    review, _ = AIAccountReview.objects.get_or_create(
        subject_type=subject_type,
        subject_id=profile.id,
        defaults={
            "subject_user_email": payload.get("user", {}).get("email", ""),
            "subject_display_name": payload.get("company_name") or payload.get("user", {}).get("email", ""),
        },
    )

    verdict = openai_client.classify_profile(payload)
    final_decision = _decide(verdict)
    review.decision = final_decision
    review.confidence = float(verdict.get("confidence") or 0)
    review.rationale = verdict.get("rationale", "")
    review.evidence = payload
    review.recommended_actions = verdict.get("recommended_actions", []) or []
    review.openai_raw = verdict.get("_raw", {})
    review.ai_model = verdict.get("_model", "")
    review.decided_at = timezone.now()

    try:
        if subject_type == "consultant":
            applied = _apply_actions_to_consultant(profile, final_decision)
        else:
            applied = _apply_actions_to_breeder(profile, final_decision)
        review.applied_actions = applied
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to apply AI decision")
        review.decision = "error"
        review.error = str(exc)

    review.save()

    AdminAuditLog.objects.create(
        actor=None,
        action=f"ai_review.{review.decision}",
        target_type=subject_type,
        target_id=str(profile.id),
        details={
            "confidence": review.confidence,
            "red_flags": verdict.get("red_flags", []),
            "applied_actions": review.applied_actions,
        },
    )

    if review.decision in ("flagged", "error"):
        _raise_flag(review, verdict)

    return review


def _raise_flag(review: AIAccountReview, verdict: dict) -> None:
    severity = "critical" if review.decision == "error" else "warning"
    red_flags = verdict.get("red_flags") or []
    reason_text = "; ".join(red_flags) if red_flags else (review.rationale or "AI requested human review.")
    recommended = next(
        (a for a in (verdict.get("recommended_actions") or []) if a.get("action") in {"request_more_info", "deactivate", "reject", "approve"}),
        None,
    )
    recommended_solution = (
        recommended.get("reason") if recommended else "Open the review and apply the appropriate decision."
    )

    flag = AIFlag.objects.create(
        review=review,
        severity=severity,
        reason=reason_text,
        recommended_solution=recommended_solution,
        applied_solution="\n".join(
            f"{a.get('action')}: {a.get('target')} {a.get('id')}" for a in review.applied_actions
        ),
    )

    super_admins: Iterable[str] = getattr(settings, "SUPERADMIN_EMAILS", [])

    subject = (
        f"[Aqua AI] {review.decision.upper()} — {review.subject_type} "
        f"{review.subject_display_name or review.subject_id}"
    )
    text_body = (
        f"Severity: {severity}\n"
        f"Subject : {review.subject_type} {review.subject_id}\n"
        f"Email   : {review.subject_user_email}\n\n"
        f"Why flagged:\n{reason_text}\n\n"
        f"AI rationale:\n{review.rationale}\n\n"
        f"Recommended next step:\n{recommended_solution}\n\n"
        f"Already applied automatically:\n{flag.applied_solution or '(no live action taken)'}\n"
    )
    delivered_email = email_client.send_admin_email(subject, text_body, recipients=super_admins)
    delivered_slack = slack_client.post_message(
        text=subject,
        blocks=[
            {"type": "header", "text": {"type": "plain_text", "text": subject[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why flagged*\n{reason_text}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*AI rationale*\n{review.rationale}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Recommended*\n{recommended_solution}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"Already applied: {flag.applied_solution or '(none)'}"},
            ]},
        ],
    )
    flag.notified_emails = list(super_admins) if delivered_email else []
    flag.notified_slack = delivered_slack
    flag.save(update_fields=["notified_emails", "notified_slack"])


def review_pending(limit: int = 100) -> List[AIAccountReview]:
    """Find profiles in the main DB that have not been AI-reviewed yet and review them."""
    reviewed_consultant_ids = set(
        AIAccountReview.objects.filter(subject_type="consultant").values_list("subject_id", flat=True)
    )
    reviewed_breeder_ids = set(
        AIAccountReview.objects.filter(subject_type="breeder").values_list("subject_id", flat=True)
    )

    consultants = (
        ExternalConsultantProfile.objects.filter(admin_status__in=["pending", ""])
        .exclude(id__in=reviewed_consultant_ids)[:limit]
    )
    breeders = (
        ExternalBreederProfile.objects.filter(is_verified=False)
        .exclude(id__in=reviewed_breeder_ids)[:limit]
    )

    out: List[AIAccountReview] = []
    for c in consultants:
        try:
            out.append(review_subject(c))
        except Exception:  # pragma: no cover
            logger.exception("consultant review failed for %s", c.id)
    for b in breeders:
        try:
            out.append(review_subject(b))
        except Exception:  # pragma: no cover
            logger.exception("breeder review failed for %s", b.id)
    return out
