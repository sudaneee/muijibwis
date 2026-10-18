"""Give every existing account a role profile.

Until now a user's section was kept in `first_name` ("all" = every section)
and their job was only implied by the username. Mapping (agreed defaults; an
administrator can change any of them on the Users page):

  superuser or first_name "all"         -> Administrator, all sections
  hos*                                  -> Head of Section
  pc                                    -> Head of Section (Principal)
  exo*, aexo*, examsOfficer*            -> Exams Officer
  everything else (ahos*, vp*, exth...) -> Staff

The role-named accounts are flagged as legacy shared accounts so the
administrator can see which ones to retire once staff have personal accounts.
The temporary "Exams Officer" group from migration 0018 is replaced by roles.
"""
from django.db import migrations

LEGACY_SHARED = {
    'admin', 'tempo', 'hosieq', 'ahosieq', 'exoieq', 'aexoieq', 'hosied', 'ahosied', 'exoied',
    'aexoied', 'hosnpd', 'ahosnpd', 'exonpd', 'aexonpd', 'hosnpq', 'ahosnpq', 'exonpq', 'aexonpq',
    'pc', 'vpadmin', 'exoss', 'aexoss', 'vpacad', 'exth', 'examsOfficerTH', 'examsOfficerMA',
}


def role_for(user):
    name = user.username
    if user.is_superuser or user.first_name == 'all':
        return 'admin'
    if name.startswith('hos') or name == 'pc':
        return 'head'
    if name.startswith(('exo', 'aexo', 'examsOfficer')):
        return 'exams'
    return 'staff'


def create_profiles(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    Group = apps.get_model('auth', 'Group')
    Section = apps.get_model('src', 'Section')
    UserProfile = apps.get_model('src', 'UserProfile')
    sections = {s.section_name: s for s in Section.objects.all()}
    for user in User.objects.all():
        if UserProfile.objects.filter(user=user).exists():
            continue
        role = role_for(user)
        UserProfile.objects.create(
            user=user, role=role,
            section=None if role == 'admin' else sections.get(user.first_name),
            is_legacy_shared=user.username in LEGACY_SHARED,
        )
    Group.objects.filter(name='Exams Officer').delete()


def remove_profiles(apps, schema_editor):
    UserProfile = apps.get_model('src', 'UserProfile')
    Group = apps.get_model('auth', 'Group')
    User = apps.get_model('auth', 'User')
    group, _ = Group.objects.get_or_create(name='Exams Officer')
    for profile in UserProfile.objects.filter(role='exams'):
        User.objects.get(pk=profile.user_id).groups.add(group)
    UserProfile.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('src', '0019_roles_audit_soft_delete'),
    ]

    operations = [
        migrations.RunPython(create_profiles, remove_profiles),
    ]
