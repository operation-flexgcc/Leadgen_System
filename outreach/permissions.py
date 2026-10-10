from .models import Profile


def is_account_executive(user):
    return bool(getattr(user, "is_authenticated", False) and
                getattr(getattr(user, "profile", None), "role", None) == Profile.Role.ACCOUNT_EXECUTIVE)


def can_view_crm(user):
    return is_manager(user) or is_account_executive(user)


def can_edit_lead(user, lead):
    return is_manager(user) or (is_account_executive(user) and lead.assigned_to_id == user.pk)


def is_submission_user(user):
    return getattr(getattr(user, "profile", None), "role", None) in {Profile.Role.INTERN, Profile.Role.INSIDE_SALES}


def is_manager(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    try:
        return user.profile.role in {Profile.Role.MANAGER, Profile.Role.SYSTEM_ADMIN}
    except Profile.DoesNotExist:
        return False


def is_system_admin(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    try:
        return user.profile.role == Profile.Role.SYSTEM_ADMIN
    except Profile.DoesNotExist:
        return False
