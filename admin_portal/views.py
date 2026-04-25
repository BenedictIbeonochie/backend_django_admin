import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.hashers import make_password
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from .forms import AcceptInviteForm, AdminInviteForm, EmailLoginForm, FlagResolveForm
from .models import (
    AdminAuditLog,
    AdminInvite,
    AdminUser,
    AIAccountReview,
    AIFlag,
    DailyReport,
)
from .permissions import admin_required, super_admin_required
from .services import build_and_deliver_daily_report, review_subject
from .services.email_client import send_admin_email


# --------------------------------------------------------------------------- auth

def login_view(request):
    if request.user.is_authenticated:
        return redirect("admin_portal:dashboard")
    form = EmailLoginForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        if not user.is_active:
            messages.error(request, "This admin account is disabled.")
        else:
            login(request, user)
            return redirect("admin_portal:dashboard")
    return render(request, "admin_portal/login.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("admin_portal:login")


# --------------------------------------------------------------------------- dashboard

@admin_required
def dashboard(request):
    today = timezone.now().date()
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_qs = AIAccountReview.objects.filter(decided_at__gte=start)

    stats = today_qs.aggregate(
        approved=Count("id", filter=Q(decision="approved")),
        rejected=Count("id", filter=Q(decision="rejected")),
        flagged=Count("id", filter=Q(decision="flagged")),
        pending=Count("id", filter=Q(decision="pending")),
    )
    stats["open_flags"] = AIFlag.objects.filter(resolved=False).count()
    stats["total_today"] = stats["approved"] + stats["rejected"] + stats["flagged"]

    context = {
        "today": today,
        "stats": stats,
        "recent_reviews": AIAccountReview.objects.all()[:10],
        "open_flags": AIFlag.objects.filter(resolved=False).select_related("review")[:10],
        "latest_report": DailyReport.objects.first(),
    }
    return render(request, "admin_portal/dashboard.html", context)


# --------------------------------------------------------------------------- reviews

@admin_required
def review_list(request):
    qs = AIAccountReview.objects.all()
    decision = request.GET.get("decision")
    subject = request.GET.get("subject")
    search = request.GET.get("q", "").strip()
    if decision:
        qs = qs.filter(decision=decision)
    if subject:
        qs = qs.filter(subject_type=subject)
    if search:
        qs = qs.filter(
            Q(subject_user_email__icontains=search) | Q(subject_display_name__icontains=search)
        )
    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "admin_portal/review_list.html",
        {
            "page": page,
            "decision": decision or "",
            "subject": subject or "",
            "search": search,
        },
    )


@admin_required
def review_detail(request, review_id):
    review = get_object_or_404(AIAccountReview, id=review_id)
    flags = review.flags.all()
    return render(
        request,
        "admin_portal/review_detail.html",
        {"review": review, "flags": flags},
    )


@admin_required
@require_POST
def review_rerun(request, review_id):
    review = get_object_or_404(AIAccountReview, id=review_id)
    from .models import ExternalBreederProfile, ExternalConsultantProfile

    Model = ExternalConsultantProfile if review.subject_type == "consultant" else ExternalBreederProfile
    try:
        profile = Model.objects.get(id=review.subject_id)
    except Model.DoesNotExist:
        raise Http404("Underlying profile no longer exists in the main backend.")
    review_subject(profile)
    AdminAuditLog.objects.create(
        actor=request.user,
        action="review.rerun",
        target_type=review.subject_type,
        target_id=str(review.subject_id),
        details={},
    )
    messages.success(request, "AI review re-run.")
    return redirect("admin_portal:review_detail", review_id=review_id)


# --------------------------------------------------------------------------- flags

@admin_required
def flag_list(request):
    qs = AIFlag.objects.select_related("review")
    status = request.GET.get("status", "open")
    if status == "open":
        qs = qs.filter(resolved=False)
    elif status == "resolved":
        qs = qs.filter(resolved=True)
    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "admin_portal/flag_list.html", {"page": page, "status": status})


@admin_required
def flag_detail(request, flag_id):
    flag = get_object_or_404(AIFlag.objects.select_related("review"), id=flag_id)
    form = FlagResolveForm()
    return render(request, "admin_portal/flag_detail.html", {"flag": flag, "form": form})


@admin_required
@require_POST
def flag_resolve(request, flag_id):
    flag = get_object_or_404(AIFlag, id=flag_id)
    form = FlagResolveForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Resolution notes are required.")
        return redirect("admin_portal:flag_detail", flag_id=flag_id)
    flag.resolved = True
    flag.resolved_at = timezone.now()
    flag.resolved_by = request.user
    flag.resolution_notes = form.cleaned_data["resolution_notes"]
    flag.save()
    AdminAuditLog.objects.create(
        actor=request.user,
        action="flag.resolve",
        target_type="flag",
        target_id=str(flag.id),
        details={"notes": flag.resolution_notes[:500]},
    )
    messages.success(request, "Flag marked as resolved.")
    return redirect("admin_portal:flag_detail", flag_id=flag_id)


# --------------------------------------------------------------------------- daily reports

@admin_required
def daily_report_list(request):
    paginator = Paginator(DailyReport.objects.all(), 30)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "admin_portal/daily_report_list.html", {"page": page})


@admin_required
def daily_report_detail(request, report_id):
    report = get_object_or_404(DailyReport, id=report_id)
    decisions = (report.details or {}).get("decisions", [])
    return render(
        request,
        "admin_portal/daily_report_detail.html",
        {"report": report, "decisions": decisions},
    )


@super_admin_required
@require_POST
def daily_report_run_now(request):
    report = build_and_deliver_daily_report()
    AdminAuditLog.objects.create(
        actor=request.user,
        action="daily_report.run",
        target_type="daily_report",
        target_id=str(report.id),
        details={"date": report.report_date.isoformat()},
    )
    messages.success(request, f"Daily report regenerated and sent for {report.report_date}.")
    return redirect("admin_portal:daily_report_detail", report_id=report.id)


# --------------------------------------------------------------------------- admin team

@super_admin_required
def admin_user_list(request):
    users = AdminUser.objects.all().order_by("-is_platform_super_admin", "email")
    invites = AdminInvite.objects.filter(revoked=False, accepted_at__isnull=True).order_by("-created_at")
    return render(
        request,
        "admin_portal/admin_user_list.html",
        {"users": users, "invites": invites},
    )


@super_admin_required
def admin_user_invite(request):
    form = AdminInviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        invite = form.save(commit=False)
        invite.created_by = request.user
        invite.token = secrets.token_urlsafe(32)
        invite.expires_at = timezone.now() + timedelta(days=7)
        invite.save()
        accept_url = request.build_absolute_uri(
            reverse("admin_portal:invite_accept", args=[invite.token])
        )
        send_admin_email(
            subject="You've been invited to the Aqua AI admin",
            text_body=(
                f"{request.user.email} has invited you to the Aqua AI admin control plane.\n\n"
                f"Accept here (link expires {invite.expires_at:%Y-%m-%d %H:%M UTC}):\n{accept_url}\n"
            ),
            recipients=[invite.email],
        )
        AdminAuditLog.objects.create(
            actor=request.user,
            action="admin.invite",
            target_type="invite",
            target_id=str(invite.id),
            details={"email": invite.email},
        )
        messages.success(request, f"Invite sent to {invite.email}.")
        return redirect("admin_portal:admin_user_list")
    return render(request, "admin_portal/invite_form.html", {"form": form})


@super_admin_required
@require_POST
def admin_user_revoke(request, user_id):
    target = get_object_or_404(AdminUser, id=user_id)
    if target.email.lower() in {e.lower() for e in settings.SUPERADMIN_EMAILS}:
        messages.error(request, "Super admins cannot be revoked from the UI.")
        return redirect("admin_portal:admin_user_list")
    if target == request.user:
        messages.error(request, "You cannot revoke yourself.")
        return redirect("admin_portal:admin_user_list")
    target.is_active = False
    target.save(update_fields=["is_active"])
    AdminAuditLog.objects.create(
        actor=request.user,
        action="admin.revoke",
        target_type="admin_user",
        target_id=str(target.id),
        details={"email": target.email},
    )
    messages.success(request, f"Revoked {target.email}.")
    return redirect("admin_portal:admin_user_list")


@super_admin_required
@require_POST
def invite_cancel(request, invite_id):
    invite = get_object_or_404(AdminInvite, id=invite_id)
    invite.revoked = True
    invite.revoked_at = timezone.now()
    invite.save(update_fields=["revoked", "revoked_at"])
    AdminAuditLog.objects.create(
        actor=request.user,
        action="admin.invite.cancel",
        target_type="invite",
        target_id=str(invite.id),
        details={"email": invite.email},
    )
    messages.success(request, f"Invite for {invite.email} cancelled.")
    return redirect("admin_portal:admin_user_list")


@require_http_methods(["GET", "POST"])
def invite_accept(request, token):
    invite = get_object_or_404(AdminInvite, token=token)
    if not invite.is_pending:
        return render(request, "admin_portal/invite_invalid.html", {"invite": invite})
    form = AcceptInviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user, created = AdminUser.objects.get_or_create(
            email=invite.email.lower(),
            defaults={
                "full_name": form.cleaned_data["full_name"] or invite.full_name,
                "invited_by": invite.created_by,
                "is_staff": True,
            },
        )
        user.password = make_password(form.cleaned_data["password1"])
        if not created and not user.is_active:
            user.is_active = True
        user.save()
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])
        AdminAuditLog.objects.create(
            actor=user,
            action="admin.invite.accept",
            target_type="admin_user",
            target_id=str(user.id),
            details={"email": user.email},
        )
        messages.success(request, "Account created. Sign in to continue.")
        return redirect("admin_portal:login")
    return render(request, "admin_portal/invite_accept.html", {"invite": invite, "form": form})
