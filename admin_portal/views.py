"""All admin-portal views.

Layout:
- Auth: login_view, logout_view, invite_accept
- Dashboard: dashboard
- AI reviews: review_list, review_detail, review_rerun
- Flags:    flag_list, flag_detail, flag_resolve
- Reports:  daily_report_list, daily_report_detail, daily_report_run_now
- Team:     admin_user_list, admin_user_invite, admin_user_revoke, invite_cancel
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import AcceptInviteForm, AdminInviteForm, EmailLoginForm, FlagResolveForm
from .models import (
    AdminInvite,
    AdminUser,
    AIAccountReview,
    AIFlag,
    DailyReport,
    ExternalBreederProfile,
    ExternalConsultantProfile,
    ExternalUser,
)
from .permissions import admin_required, super_admin_required
from .services import audit
from .services.notifier import notify_invite
from .services.reporting import build_report_for
from .services.review_runner import process_pending, run_review


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_view(request):
    if request.user.is_authenticated:
        return redirect("admin_portal:dashboard")
    form = EmailLoginForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data["username"].lower(),
            password=form.cleaned_data["password"],
        )
        if user and user.is_active:
            login(request, user)
            audit.record(user, "login")
            return redirect(request.GET.get("next") or "admin_portal:dashboard")
        messages.error(request, "Invalid email or password.")
    return render(request, "admin_portal/login.html", {"form": form})


@login_required(login_url="admin_portal:login")
def logout_view(request):
    audit.record(request.user, "logout")
    logout(request)
    return redirect("admin_portal:login")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@admin_required
def dashboard(request):
    today = timezone.now().date()
    last_7 = today - timedelta(days=6)
    qs = AIAccountReview.objects.all()

    counts = qs.aggregate(
        total=Count("id"),
        approved=Count("id", filter=Q(decision="approved")),
        rejected=Count("id", filter=Q(decision="rejected")),
        flagged=Count("id", filter=Q(decision="flagged")),
        pending=Count("id", filter=Q(decision="pending")),
    )
    today_qs = qs.filter(created_at__date=today)
    today_counts = today_qs.aggregate(
        approved=Count("id", filter=Q(decision="approved")),
        rejected=Count("id", filter=Q(decision="rejected")),
        flagged=Count("id", filter=Q(decision="flagged")),
        pending=Count("id", filter=Q(decision="pending")),
    )

    recent_reviews = qs[:10]
    open_flags = AIFlag.objects.filter(resolved=False).select_related("review")[:10]
    last_reports = DailyReport.objects.filter(report_date__gte=last_7)

    ai_key_set = bool(settings.OPENAI_API_KEY) and "REPLACE" not in settings.OPENAI_API_KEY.upper()

    return render(request, "admin_portal/dashboard.html", {
        "counts": counts,
        "today_counts": today_counts,
        "recent_reviews": recent_reviews,
        "open_flags": open_flags,
        "last_reports": last_reports,
        "ai_key_set": ai_key_set,
    })


# ---------------------------------------------------------------------------
# AI reviews
# ---------------------------------------------------------------------------

@admin_required
def review_list(request):
    qs = AIAccountReview.objects.all()
    decision = request.GET.get("decision")
    subject = request.GET.get("subject")
    q = (request.GET.get("q") or "").strip()
    if decision in {"approved", "rejected", "flagged", "pending", "error"}:
        qs = qs.filter(decision=decision)
    if subject in {"breeder", "consultant"}:
        qs = qs.filter(subject_type=subject)
    if q:
        qs = qs.filter(
            Q(subject_user_email__icontains=q) | Q(subject_display_name__icontains=q)
        )
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "admin_portal/review_list.html", {
        "page": page,
        "decision": decision or "",
        "subject": subject or "",
        "q": q,
    })


@admin_required
def review_detail(request, review_id):
    review = get_object_or_404(AIAccountReview, pk=review_id)
    profile = _load_external_profile(review)
    user = None
    if profile:
        try:
            user = ExternalUser.objects.get(pk=profile.user_id)
        except ExternalUser.DoesNotExist:
            user = None
    flags = review.flags.all().order_by("-created_at")
    return render(request, "admin_portal/review_detail.html", {
        "review": review,
        "profile": profile,
        "external_user": user,
        "flags": flags,
    })


@super_admin_required
def review_rerun(request, review_id):
    review = get_object_or_404(AIAccountReview, pk=review_id)
    profile = _load_external_profile(review)
    if not profile:
        messages.error(request, "External profile no longer exists; cannot re-run.")
        return redirect("admin_portal:review_detail", review_id=review.id)
    try:
        user = ExternalUser.objects.get(pk=profile.user_id)
    except ExternalUser.DoesNotExist:
        messages.error(request, "External user no longer exists.")
        return redirect("admin_portal:review_detail", review_id=review.id)
    if request.method == "POST":
        run_review(review.subject_type, profile, user)
        audit.record(request.user, "review.rerun", target_type="review", target_id=review.id)
        messages.success(request, "Review re-ran with the latest profile state.")
    return redirect("admin_portal:review_detail", review_id=review.id)


def _load_external_profile(review: AIAccountReview):
    Model = ExternalConsultantProfile if review.subject_type == "consultant" else ExternalBreederProfile
    try:
        return Model.objects.get(pk=review.subject_id)
    except Model.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

@admin_required
def flag_list(request):
    qs = AIFlag.objects.select_related("review").all()
    severity = request.GET.get("severity")
    show_resolved = request.GET.get("resolved") == "1"
    if severity in {"info", "warning", "critical"}:
        qs = qs.filter(severity=severity)
    if not show_resolved:
        qs = qs.filter(resolved=False)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "admin_portal/flag_list.html", {
        "page": page, "severity": severity or "", "show_resolved": show_resolved,
    })


@admin_required
def flag_detail(request, flag_id):
    flag = get_object_or_404(AIFlag.objects.select_related("review"), pk=flag_id)
    return render(request, "admin_portal/flag_detail.html", {
        "flag": flag,
        "form": FlagResolveForm(),
    })


@admin_required
def flag_resolve(request, flag_id):
    flag = get_object_or_404(AIFlag, pk=flag_id)
    if request.method != "POST":
        return redirect("admin_portal:flag_detail", flag_id=flag.id)
    form = FlagResolveForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Resolution notes are required.")
        return redirect("admin_portal:flag_detail", flag_id=flag.id)
    flag.resolved = True
    flag.resolved_by = request.user
    flag.resolved_at = timezone.now()
    flag.resolution_notes = form.cleaned_data["resolution_notes"]
    flag.save()
    audit.record(request.user, "flag.resolve", target_type="flag", target_id=flag.id)
    messages.success(request, "Flag resolved.")
    return redirect("admin_portal:flag_detail", flag_id=flag.id)


# ---------------------------------------------------------------------------
# Daily reports
# ---------------------------------------------------------------------------

@admin_required
def daily_report_list(request):
    page = Paginator(DailyReport.objects.all(), 30).get_page(request.GET.get("page"))
    return render(request, "admin_portal/daily_report_list.html", {"page": page})


@admin_required
def daily_report_detail(request, report_id):
    report = get_object_or_404(DailyReport, pk=report_id)
    review_ids = (report.details or {}).get("review_ids", [])
    reviews = AIAccountReview.objects.filter(id__in=review_ids).order_by("-created_at")
    return render(request, "admin_portal/daily_report_detail.html", {
        "report": report, "reviews": reviews,
    })


@super_admin_required
def daily_report_run_now(request):
    if request.method == "POST":
        report = build_report_for()
        audit.record(request.user, "report.run_now", target_type="daily_report", target_id=report.id)
        messages.success(request, f"Report generated for {report.report_date}.")
        return redirect("admin_portal:daily_report_detail", report_id=report.id)
    return redirect("admin_portal:daily_report_list")


# ---------------------------------------------------------------------------
# Team management (super admins only)
# ---------------------------------------------------------------------------

@super_admin_required
def admin_user_list(request):
    users = AdminUser.objects.order_by("email")
    invites = AdminInvite.objects.filter(accepted_at__isnull=True, revoked=False)
    return render(request, "admin_portal/admin_user_list.html", {
        "users": users,
        "invites": invites,
        "invite_form": AdminInviteForm(),
    })


@super_admin_required
def admin_user_invite(request):
    if request.method != "POST":
        return redirect("admin_portal:admin_user_list")
    form = AdminInviteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid invite form.")
        return redirect("admin_portal:admin_user_list")
    email = form.cleaned_data["email"].lower().strip()
    if AdminUser.objects.filter(email=email).exists():
        messages.warning(request, f"{email} is already an admin.")
        return redirect("admin_portal:admin_user_list")
    invite = form.save(commit=False)
    invite.email = email
    invite.created_by = request.user
    invite.token = secrets.token_urlsafe(32)
    invite.expires_at = timezone.now() + timedelta(days=7)
    invite.save()
    accept_url = request.build_absolute_uri(
        reverse("admin_portal:invite_accept", args=[invite.token])
    )
    notify_invite(invite, accept_url)
    audit.record(request.user, "invite.create", target_type="invite", target_id=invite.id, email=email)
    messages.success(request, f"Invite sent to {email}.")
    return redirect("admin_portal:admin_user_list")


@super_admin_required
def admin_user_revoke(request, user_id):
    target = get_object_or_404(AdminUser, pk=user_id)
    if target.is_super_admin:
        messages.error(request, "You cannot revoke a platform super-admin.")
        return redirect("admin_portal:admin_user_list")
    if target.pk == request.user.pk:
        messages.error(request, "You cannot revoke your own account.")
        return redirect("admin_portal:admin_user_list")
    if request.method == "POST":
        target.is_active = False
        target.save(update_fields=["is_active"])
        audit.record(request.user, "admin.revoke", target_type="admin_user", target_id=target.id)
        messages.success(request, f"{target.email} deactivated.")
    return redirect("admin_portal:admin_user_list")


@super_admin_required
def invite_cancel(request, invite_id):
    invite = get_object_or_404(AdminInvite, pk=invite_id, accepted_at__isnull=True)
    if request.method == "POST":
        invite.revoked = True
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked", "revoked_at"])
        audit.record(request.user, "invite.cancel", target_type="invite", target_id=invite.id)
        messages.success(request, "Invite cancelled.")
    return redirect("admin_portal:admin_user_list")


def invite_accept(request, token):
    invite = get_object_or_404(AdminInvite, token=token)
    if not invite.is_pending:
        return render(request, "admin_portal/invite_invalid.html", {"invite": invite})
    form = AcceptInviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if AdminUser.objects.filter(email=invite.email).exists():
            messages.error(request, "An admin already exists for this email.")
            return redirect("admin_portal:login")
        user = AdminUser.objects.create_user(
            email=invite.email,
            password=form.cleaned_data["password1"],
            full_name=form.cleaned_data.get("full_name") or invite.full_name,
            is_staff=True,
            is_platform_super_admin=False,
            invited_by=invite.created_by,
        )
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])
        login(request, user)
        audit.record(user, "invite.accept", target_type="invite", target_id=invite.id)
        messages.success(request, "Welcome — your account is active.")
        return redirect("admin_portal:dashboard")
    return render(request, "admin_portal/invite_accept.html", {"invite": invite, "form": form})


# ---------------------------------------------------------------------------
# Optional: trigger one-shot processing from the UI (super admin)
# ---------------------------------------------------------------------------

@super_admin_required
def process_now(request):
    if request.method == "POST":
        counts = process_pending(limit_per_type=25)
        audit.record(request.user, "ai.process_now", **counts)
        messages.success(request, f"Reviewed: {counts['breeder']} breeders, {counts['consultant']} consultants.")
    return redirect("admin_portal:review_list")
