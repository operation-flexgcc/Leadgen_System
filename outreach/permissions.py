from .models import Profile


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
