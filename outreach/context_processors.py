from .permissions import is_manager, is_system_admin


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
    }
