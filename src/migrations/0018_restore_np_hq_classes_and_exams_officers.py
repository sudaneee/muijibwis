"""Phase 1 data repair, agreed with the school on 2026-10-18.

1. The NP HQ section was deleted and recreated on 2025-09-19. The deletion
   left its 19 existing classes (created as "... -- NP HQ", per the admin log)
   with no section, hiding 2022-2025 results from NP HQ staff. Re-link them to
   NP HQ and mark them archived (no 2025/26 activity; the current NP HQ
   roster lives in the recreated classes).
2. Create an "Exams Officer" group for the existing exams-officer accounts;
   members can use the data review screens for their own section.

Each step only touches rows that still match what was found, so running this
on any other database (tests, a fresh install) changes nothing.
"""
from django.db import migrations

NP_HQ_CLASS_IDS = [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112,
                   113, 114, 137, 138, 167, 168, 177]

EXAMS_OFFICER_USERNAMES = [
    'exoieq', 'aexoieq', 'exoied', 'aexoied', 'exonpd', 'aexonpd',
    'exonpq', 'aexonpq', 'exoss', 'aexoss', 'examsOfficerTH', 'examsOfficerMA',
]


def restore_np_hq(apps, schema_editor):
    Section = apps.get_model('src', 'Section')
    StudentClass = apps.get_model('src', 'StudentClass')
    ReviewDecision = apps.get_model('src', 'ReviewDecision')

    sections = list(Section.objects.filter(section_name='NP HQ'))
    if len(sections) != 1:
        return
    np_hq = sections[0]
    classes = list(StudentClass.objects.filter(id__in=NP_HQ_CLASS_IDS, section__isnull=True))
    if not classes:
        return
    ids = [c.id for c in classes]
    StudentClass.objects.filter(id__in=ids).update(section=np_hq, is_archived=True)
    ReviewDecision.objects.create(
        kind='class_section',
        target='np-hq-restore-2025-09-19',
        action='restore',
        summary=f'Re-linked {len(ids)} classes to NP HQ and archived them '
                '(section was deleted and recreated on 2025-09-19).',
        before={'classes': [{'id': c.id, 'name': c.name, 'section_id': None,
                             'is_archived': c.is_archived} for c in classes]},
        after={'section_id': np_hq.id, 'is_archived': True, 'class_ids': ids},
        note='Data migration 0018, approved by the school.',
    )


def unrestore_np_hq(apps, schema_editor):
    StudentClass = apps.get_model('src', 'StudentClass')
    ReviewDecision = apps.get_model('src', 'ReviewDecision')
    for decision in ReviewDecision.objects.filter(target='np-hq-restore-2025-09-19'):
        for c in decision.before['classes']:
            StudentClass.objects.filter(id=c['id']).update(
                section=None, is_archived=c['is_archived'])
        decision.delete()


def add_exams_officers(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    User = apps.get_model('auth', 'User')
    group, _ = Group.objects.get_or_create(name='Exams Officer')
    for user in User.objects.filter(username__in=EXAMS_OFFICER_USERNAMES):
        user.groups.add(group)


def remove_exams_officers(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name='Exams Officer').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('src', '0017_class_archive_and_review_log'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(restore_np_hq, unrestore_np_hq),
        migrations.RunPython(add_exams_officers, remove_exams_officers),
    ]
