from .models import Profile
from .permissions import (
    active_role,
    assigned_roles,
    can_view_all_outreach,
    can_view_crm,
    can_view_user_access,
    is_account_executive,
    is_ai_user,
    is_manager,
    is_submission_user,
    is_system_admin,
)


def current_role(request):
    role_label = ""
    if getattr(request.user, "is_authenticated", False):
        try:
            role_label = dict(Profile.Role.choices).get(active_role(request.user), "Choose a role")
        except Exception:
            role_label = "User"
    return {
        "current_user_is_manager": is_manager(request.user),
        "current_user_is_system_admin": is_system_admin(request.user),
        "current_user_is_ai_user": is_ai_user(request.user),
        "current_user_is_read_only": is_ai_user(request.user),
        "current_user_can_view_all_outreach": can_view_all_outreach(request.user),
        "current_user_can_view_users": can_view_user_access(request.user),
        "current_user_role_label": role_label,
        "current_user_has_active_role": bool(active_role(request.user)),
        "current_user_has_multiple_roles": len(assigned_roles(request.user)) > 1,
        "current_user_can_view_crm": can_view_crm(request.user),
        "current_user_is_executive": is_account_executive(request.user),
        "current_user_is_submitter": is_submission_user(request.user),
    }
