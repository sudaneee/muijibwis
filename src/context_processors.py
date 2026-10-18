from src.access import PERMISSIONS, has_perm, profile, scoped_classes, sees_all_sections, user_section
from src.models import Session, Term


def site_settings(request):
    user = request.user
    p = profile(user)
    return {
        # Data-entry dropdowns: the user's section, archived classes hidden.
        'classes': scoped_classes(user),
        # Report pages also offer archived classes so old results stay viewable.
        'archived_classes': scoped_classes(user, include_archived=True).filter(is_archived=True),
        'terms': Term.objects.all(),
        'sessions': Session.objects.all(),
        # What the menu may show; every view checks again on the server.
        'can': {perm: has_perm(user, perm) for perm in PERMISSIONS},
        'can_review': has_perm(user, 'review_data'),
        'sees_all': sees_all_sections(user),
        'my_section': user_section(user),
        'my_role': p.get_role_display() if p else ('Administrator' if user.is_authenticated and user.is_superuser else ''),
    }
