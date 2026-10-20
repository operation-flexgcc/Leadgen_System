from django.db.models import Q

from .models import Profile


def assigned_roles(user):
    if not getattr(user, "is_authenticated", False):
        return []
    if hasattr(user, "_assigned_roles"):
        return user._assigned_roles
    try:
        return user.profile.assigned_roles
    except Profile.DoesNotExist:
        return []


def active_role(user):
    roles = assigned_roles(user)
    # Browser middleware and token authentication always set the active role.
    # Non-request callers retain the existing primary-role behavior.
    role = getattr(user, "_active_role", roles[0] if roles else None)
    return role if role in roles else None


def role_membership_query(roles, prefix="profile__"):
    return Q(**{prefix + "role__in": roles}) | Q(**{prefix + "additional_roles__role__in": roles})


def is_account_executive(user):
    return active_role(user) == Profile.Role.ACCOUNT_EXECUTIVE


def can_view_crm(user):
    return can_view_all_outreach(user) or is_account_executive(user)


def can_edit_lead(user, lead):
    return is_manager(user) or (is_account_executive(user) and lead.assigned_to_id == user.pk)


def is_submission_user(user):
    return active_role(user) in {Profile.Role.INTERN, Profile.Role.INSIDE_SALES}


def is_manager(user) -> bool:
    return active_role(user) in {Profile.Role.MANAGER, Profile.Role.SYSTEM_ADMIN}


def is_system_admin(user) -> bool:
    return active_role(user) == Profile.Role.SYSTEM_ADMIN


def is_ai_user(user) -> bool:
    return active_role(user) == Profile.Role.AI_USER


def can_view_all_outreach(user) -> bool:
    """Return whether the active role has organization-wide read visibility."""

    return is_manager(user) or is_ai_user(user)


def can_view_user_access(user) -> bool:
    """System admins manage access; AI users may inspect it read-only."""

    return is_system_admin(user) or is_ai_user(user)
