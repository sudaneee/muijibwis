"""Roles, permissions and section scoping, enforced on the server.

Every view that reads a class, student or result id from a request checks it
against `scoped_classes`, so changing a form value or URL cannot reach another
section's records.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from src.models import StudentClass, UserProfile

ADMIN, HEAD, EXAMS, STAFF = UserProfile.ADMIN, UserProfile.HEAD, UserProfile.EXAMS, UserProfile.STAFF

PERMISSIONS = {
    'view_reports':    {ADMIN, HEAD, EXAMS, STAFF},
    'enter_results':   {ADMIN, HEAD, EXAMS, STAFF},
    'manage_students': {ADMIN, HEAD, EXAMS},
    'delete_records':  {ADMIN, HEAD, EXAMS},
    'review_data':     {ADMIN, HEAD, EXAMS},
    'promote':         {ADMIN, HEAD},
    'approve_results': {ADMIN, HEAD},
    'view_audit':      {ADMIN, HEAD},
    'manage_users':    {ADMIN},
}


def profile(user):
    if not user.is_authenticated:
        return None
    try:
        return user.profile
    except UserProfile.DoesNotExist:
        return None


def role(user):
    if not user.is_authenticated or not user.is_active:
        return None
    if user.is_superuser:
        return ADMIN
    p = profile(user)
    return p.role if p else None


def has_perm(user, perm):
    return role(user) in PERMISSIONS[perm]


def sees_all_sections(user):
    return role(user) == ADMIN


def user_section(user):
    p = profile(user)
    return p.section if p else None


def scoped_classes(user, include_archived=False):
    """Classes the user may work with. Users without a section get nothing."""
    if role(user) is None:
        return StudentClass.objects.none()
    if sees_all_sections(user):
        classes = StudentClass.objects.all()
    else:
        section = user_section(user)
        if section is None:
            return StudentClass.objects.none()
        classes = StudentClass.objects.filter(section=section)
    if not include_archived:
        classes = classes.filter(is_archived=False)
    return classes.select_related('section')


def scoped_class(user, class_id, include_archived=False):
    """Return the class if it is in the user's scope, otherwise refuse."""
    try:
        class_id = int(class_id)
    except (TypeError, ValueError):
        raise PermissionDenied('Choose a class.')
    found = scoped_classes(user, include_archived).filter(pk=class_id).first()
    if found is None:
        raise PermissionDenied('That class is not in your section.')
    return found


def can_review(user):
    return has_perm(user, 'review_data')


def require(perm):
    """Login + permission check. Anonymous users get the login page as before."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return render(request, 'src/welcome.html')
            if not has_perm(request.user, perm):
                raise PermissionDenied('Your role does not allow this.')
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


review_required = require('review_data')
