from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.db.models import Count, F, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import DashboardFilterForm, OutreachForm, ProspectForm, UserProvisionForm
from .models import Outreach, Profile, Prospect
from .permissions import is_manager, is_system_admin


REQUIRED_HEALTH_TABLES = {
    "auth_user",
    "django_migrations",
    "django_session",
    "django_site",
    "outreach_prospect",
    "socialaccount_socialaccount",
}


def login_landing(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "account/login.html")


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        available_tables = set(connection.introspection.table_names())
        if not REQUIRED_HEALTH_TABLES.issubset(available_tables):
            raise RuntimeError("Required database tables are unavailable.")
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)


def scoped_prospects(user):
    queryset = Prospect.objects.select_related("owner", "owner__profile")
    if is_manager(user):
        return queryset
    try:
        role = user.profile.role
    except Profile.DoesNotExist:
        return queryset.none()
    return queryset.filter(Q(owner=user) | Q(owner__isnull=True, workstream=role))


def get_scoped_prospect(user, pk):
    return get_object_or_404(scoped_prospects(user), pk=pk)


def sync_prospect_from_outreach(prospect, outreach):
    update_fields = set()
    if prospect.stage == Prospect.Stage.ELIGIBLE:
        prospect.stage = Prospect.Stage.CONTACTED
        update_fields.add("stage")
    if outreach.response and prospect.stage in {Prospect.Stage.ELIGIBLE, Prospect.Stage.CONTACTED}:
        prospect.stage = Prospect.Stage.RESPONDED
        update_fields.add("stage")
    if (
        outreach.activity_type == Outreach.ActivityType.CONNECTION_REQUEST
        and prospect.linkedin_connection_status
        in {"", Prospect.LinkedInConnectionStatus.NOT_SENT}
    ):
        prospect.linkedin_connection_status = Prospect.LinkedInConnectionStatus.REQUEST_SENT
        update_fields.add("linkedin_connection_status")
    elif outreach.activity_type == Outreach.ActivityType.CONNECTION_ACCEPTED:
        prospect.linkedin_connection_status = Prospect.LinkedInConnectionStatus.ACCEPTED
        update_fields.add("linkedin_connection_status")
    elif (
        outreach.activity_type == Outreach.ActivityType.MATERIAL_SENT
        or (
            prospect.workstream == Prospect.Workstream.LINKEDIN_OUTREACH
            and outreach.activity_type == Outreach.ActivityType.INITIAL_OUTREACH
        )
    ) and not prospect.material_shared:
        prospect.material_shared = Prospect.MaterialShared.ONE_PAGE
        update_fields.add("material_shared")
    elif outreach.activity_type == Outreach.ActivityType.FOUNDER_ESCALATION:
        prospect.founder_escalation_required = True
        if outreach.response:
            prospect.founder_escalation_notes = outreach.response
            update_fields.add("founder_escalation_notes")
        update_fields.add("founder_escalation_required")
    if update_fields:
        update_fields.add("updated_at")
        prospect.save(update_fields=update_fields)


@login_required
def dashboard(request):
    today = timezone.localdate()
    base_queryset = scoped_prospects(request.user)
    summary = base_queryset.aggregate(
        total=Count("id"),
        unassigned=Count("id", filter=Q(owner__isnull=True)),
        actions_today=Count("id", filter=Q(next_action_date=today)),
        overdue=Count("id", filter=Q(next_action_date__lt=today)),
        meetings_scheduled=Count("id", filter=Q(status=Prospect.Status.MEETING_SCHEDULED)),
    )
    status_counts = {row["status"]: row["total"] for row in base_queryset.values("status").annotate(total=Count("id"))}
    stage_counts = {row["stage"]: row["total"] for row in base_queryset.values("stage").annotate(total=Count("id"))}
    workstream_counts = {
        row["workstream"]: row["total"] for row in base_queryset.values("workstream").annotate(total=Count("id"))
    }

    filter_form = DashboardFilterForm(request.GET or None, user=request.user)
    prospects = base_queryset.annotate(outreach_count=Count("outreaches"))
    if filter_form.is_valid():
        data = filter_form.cleaned_data
        if data.get("search"):
            search = data["search"].strip()
            prospects = prospects.filter(
                Q(company_name__icontains=search)
                | Q(contact_name__icontains=search)
                | Q(contact_email__icontains=search)
                | Q(location__icontains=search)
                | Q(consulting_focus__icontains=search)
            )
        if data.get("status"):
            prospects = prospects.filter(status=data["status"])
        if data.get("stage"):
            prospects = prospects.filter(stage=data["stage"])
        if data.get("workstream"):
            prospects = prospects.filter(workstream=data["workstream"])
        if data.get("outreach_count") != "" and data.get("outreach_count") is not None:
            prospects = prospects.filter(outreach_count=int(data["outreach_count"]))
        if data.get("due") == "today":
            prospects = prospects.filter(next_action_date=today)
        elif data.get("due") == "overdue":
            prospects = prospects.filter(next_action_date__lt=today)
        elif data.get("due") == "upcoming":
            prospects = prospects.filter(next_action_date__gt=today)
        elif data.get("due") == "none":
            prospects = prospects.filter(next_action_date__isnull=True)
        if data.get("owner"):
            prospects = prospects.filter(owner=data["owner"])

    prospects = prospects.order_by(F("next_action_date").asc(nulls_last=True), "company_name")
    paginator = Paginator(prospects, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)

    context = {
        "summary": summary,
        "status_counts": status_counts,
        "status_summary": [
            (value, label, status_counts.get(value, 0)) for value, label in Prospect.Status.choices
        ],
        "stage_summary": [
            (value, label, stage_counts.get(value, 0)) for value, label in Prospect.Stage.choices
        ],
        "workstream_summary": [
            (value, label, workstream_counts.get(value, 0)) for value, label in Prospect.Workstream.choices
        ],
        "status_choices": Prospect.Status.choices,
        "filter_form": filter_form,
        "page_obj": page_obj,
        "query_string": query_params.urlencode(),
        "today": today,
        "operator_count": (
            Profile.objects.filter(role__in=Prospect.Workstream.values, user__is_active=True).count()
            if is_manager(request.user)
            else None
        ),
    }
    return render(request, "outreach/dashboard.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def prospect_create(request):
    form = ProspectForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        prospect = form.save(commit=False)
        prospect.created_by = request.user
        prospect.save()
        messages.success(request, f"{prospect.company_name} was added.")
        return redirect(prospect)
    return render(request, "outreach/prospect_form.html", {"form": form, "title": "Add prospect"})


@login_required
@require_http_methods(["GET", "POST"])
def prospect_update(request, pk):
    prospect = get_scoped_prospect(request.user, pk)
    if not is_manager(request.user) and prospect.owner_id != request.user.id:
        messages.error(request, "Claim this prospect before editing its research or follow-up details.")
        return redirect(prospect)
    form = ProspectForm(request.POST or None, instance=prospect, user=request.user)
    if request.method == "POST" and form.is_valid():
        prospect = form.save()
        messages.success(request, f"{prospect.company_name} was updated.")
        return redirect(prospect)
    return render(
        request,
        "outreach/prospect_form.html",
        {"form": form, "prospect": prospect, "title": f"Edit {prospect.company_name}"},
    )


@login_required
def prospect_detail(request, pk):
    prospect = get_scoped_prospect(request.user, pk)
    outreaches = prospect.outreaches.select_related("recorded_by").all()
    can_edit_prospect = is_manager(request.user) or prospect.owner_id == request.user.id
    return render(
        request,
        "outreach/prospect_detail.html",
        {
            "prospect": prospect,
            "outreaches": outreaches,
            "outreach_form": OutreachForm(prospect=prospect),
            "can_add_outreach": (
                can_edit_prospect
                and prospect.stage != Prospect.Stage.RESEARCH
                and outreaches.count() < 5
            ),
            "can_edit_prospect": can_edit_prospect,
            "can_claim_prospect": (
                not is_manager(request.user)
                and prospect.owner_id is None
                and prospect.workstream == request.user.profile.role
            ),
            "today": timezone.localdate(),
        },
    )


@login_required
@require_POST
def outreach_add(request, pk):
    with transaction.atomic():
        prospect = get_object_or_404(scoped_prospects(request.user).select_for_update(), pk=pk)
        if prospect.owner_id != request.user.id and not is_manager(request.user):
            messages.error(request, "Claim this prospect before recording outreach.")
            return redirect(prospect)
        if prospect.stage == Prospect.Stage.RESEARCH:
            messages.error(request, "Complete the required contact and qualification research before recording outreach.")
            return redirect(prospect)
        existing_numbers = list(prospect.outreaches.order_by("sequence_number").values_list("sequence_number", flat=True))
        if len(existing_numbers) >= 5:
            messages.error(request, "This prospect already has the maximum of five outreach records.")
            return redirect(prospect)
        form = OutreachForm(request.POST, prospect=prospect)
        if form.is_valid():
            outreach = form.save(commit=False)
            outreach.prospect = prospect
            outreach.recorded_by = request.user
            outreach.sequence_number = next(number for number in range(1, 6) if number not in existing_numbers)
            try:
                outreach.save()
            except IntegrityError:
                messages.error(request, "Another outreach was added at the same time. Please try again.")
            else:
                sync_prospect_from_outreach(prospect, outreach)
                messages.success(request, f"Outreach {outreach.sequence_number} was recorded.")
                return redirect(prospect)
    outreaches = prospect.outreaches.select_related("recorded_by").all()
    return render(
        request,
        "outreach/prospect_detail.html",
        {
            "prospect": prospect,
            "outreaches": outreaches,
            "outreach_form": form,
            "can_add_outreach": outreaches.count() < 5,
            "can_edit_prospect": is_manager(request.user) or prospect.owner_id == request.user.id,
            "can_claim_prospect": False,
            "today": timezone.localdate(),
        },
        status=400,
    )


@login_required
@require_POST
def prospect_claim(request, pk):
    if is_manager(request.user):
        messages.error(request, "Managers can assign prospects from the edit screen.")
        return redirect("dashboard")

    with transaction.atomic():
        prospect = get_object_or_404(Prospect.objects.select_for_update(), pk=pk)
        if prospect.workstream != request.user.profile.role:
            raise PermissionDenied("This prospect belongs to another outreach workstream.")
        if prospect.owner_id is None:
            prospect.owner = request.user
            prospect.save(update_fields=["owner", "updated_at"])
            messages.success(request, f"{prospect.company_name} is now assigned to you.")
        elif prospect.owner_id == request.user.id:
            messages.info(request, f"{prospect.company_name} is already assigned to you.")
        else:
            messages.error(request, "Another user has already claimed this prospect.")
            return redirect("dashboard")
    return redirect(prospect)


@login_required
@require_http_methods(["GET", "POST"])
def outreach_update(request, pk):
    outreach = get_object_or_404(Outreach.objects.select_related("prospect"), pk=pk)
    prospect = get_scoped_prospect(request.user, outreach.prospect_id)
    form = OutreachForm(request.POST or None, instance=outreach, prospect=prospect)
    if request.method == "POST" and form.is_valid():
        outreach = form.save()
        sync_prospect_from_outreach(prospect, outreach)
        messages.success(request, f"Outreach {outreach.sequence_number} was updated.")
        return redirect(prospect)
    return render(
        request,
        "outreach/outreach_form.html",
        {"form": form, "outreach": outreach, "prospect": prospect},
    )


def home(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return redirect_to_login("/dashboard/", login_url="login")


def require_system_admin(user):
    if not is_system_admin(user):
        raise PermissionDenied("System administrator access is required.")


def invalidate_user_sessions(user):
    for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
        if session.get_decoded().get(SESSION_KEY) == str(user.pk):
            session.delete()


@login_required
def user_list(request):
    require_system_admin(request.user)
    users = (
        Profile.objects.select_related("user")
        .order_by("-user__is_active", "role", "user__first_name", "user__email")
    )
    counts = {
        "active": users.filter(user__is_active=True).count(),
        "interns": users.filter(user__is_active=True, role=Profile.Role.INTERN).count(),
        "inside_sales": users.filter(user__is_active=True, role=Profile.Role.INSIDE_SALES).count(),
        "linkedin_outreach": users.filter(user__is_active=True, role=Profile.Role.LINKEDIN_OUTREACH).count(),
        "managers": users.filter(user__is_active=True, role=Profile.Role.MANAGER).count(),
        "admins": users.filter(user__is_active=True, role=Profile.Role.SYSTEM_ADMIN).count(),
    }
    return render(request, "outreach/user_list.html", {"profiles": users, "counts": counts})


@login_required
@require_http_methods(["GET", "POST"])
def user_create(request):
    require_system_admin(request.user)
    form = UserProvisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        messages.success(request, f"{user.get_full_name()} was added and can now sign in with {user.email}.")
        return redirect("user_list")
    return render(request, "outreach/user_form.html", {"form": form, "title": "Add user"})


@login_required
@require_http_methods(["GET", "POST"])
def user_update(request, pk):
    require_system_admin(request.user)
    target = get_object_or_404(Profile.objects.select_related("user"), user_id=pk).user
    form = UserProvisionForm(request.POST or None, instance=target)
    if request.method == "POST" and form.is_valid():
        new_role = form.cleaned_data["role"]
        if target == request.user and new_role != Profile.Role.SYSTEM_ADMIN:
            form.add_error("role", "You cannot remove your own system administrator access.")
        elif (
            target.profile.role == Profile.Role.SYSTEM_ADMIN
            and new_role != Profile.Role.SYSTEM_ADMIN
            and not Profile.objects.filter(role=Profile.Role.SYSTEM_ADMIN, user__is_active=True).exclude(user=target).exists()
        ):
            form.add_error("role", "Add another active system administrator before changing the last one.")
        else:
            user = form.save()
            messages.success(request, f"{user.get_full_name()} was updated.")
            return redirect("user_list")
    return render(request, "outreach/user_form.html", {"form": form, "target_user": target, "title": "Edit user"})


@login_required
@require_http_methods(["GET", "POST"])
def user_delete(request, pk):
    require_system_admin(request.user)
    target = get_object_or_404(Profile.objects.select_related("user"), user_id=pk).user
    if target == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("user_list")
    if (
        target.profile.role == Profile.Role.SYSTEM_ADMIN
        and target.is_active
        and not Profile.objects.filter(role=Profile.Role.SYSTEM_ADMIN, user__is_active=True).exclude(user=target).exists()
    ):
        messages.error(request, "Add another active system administrator before deleting the last one.")
        return redirect("user_list")
    if request.method == "POST":
        name = target.get_full_name() or target.email
        try:
            target.delete()
        except ProtectedError:
            target.is_active = False
            target.save(update_fields=["is_active"])
            invalidate_user_sessions(target)
            messages.warning(request, f"{name}'s access was removed. Their account record was retained to preserve sales history.")
        else:
            messages.success(request, f"{name} was deleted.")
        return redirect("user_list")
    return render(request, "outreach/user_confirm_delete.html", {"target_user": target})


@login_required
@require_POST
def user_restore(request, pk):
    require_system_admin(request.user)
    target = get_object_or_404(Profile.objects.select_related("user"), user_id=pk).user
    target.is_active = True
    target.save(update_fields=["is_active"])
    messages.success(request, f"{target.get_full_name() or target.email} can sign in again.")
    return redirect("user_list")
