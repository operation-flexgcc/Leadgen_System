from .permissions import can_view_crm, is_account_executive, is_manager, is_submission_user, is_system_admin


def current_role(request):
    role_label = ""
    if getattr(request.user, "is_authenticated", False):
        try:
            role_label = request.user.profile.get_role_display()
        except Exception:
            role_label = "User"
    return {
        "current_user_is_manager": is_manager(request.user),
        "current_user_is_system_admin": is_system_admin(request.user),
        "current_user_role_label": role_label,
        "current_user_can_view_crm": can_view_crm(request.user),
        "current_user_is_executive": is_account_executive(request.user),
        "current_user_is_submitter": is_submission_user(request.user),
    }
