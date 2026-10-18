"""Audit trail viewer and recycle bin (restore soft-deleted records)."""
from collections import defaultdict
from datetime import datetime, time

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone

from src import audit
from src.access import require, scoped_classes, sees_all_sections, user_section
from src.models import AuditLog, Student, StudentBehaviouralAssessment, StudentResult
from src.scoring import recompute_positions

BIN_MODELS = {
    'result': (StudentResult, 'results'),
    'behaviour': (StudentBehaviouralAssessment, 'behavioural assessments'),
    'student': (Student, 'students'),
}


@require('view_audit')
def audit_log(request):
    entries = AuditLog.objects.select_related('section')
    if not sees_all_sections(request.user):
        entries = entries.filter(section=user_section(request.user))
    f = {k: request.GET.get(k, '').strip() for k in ('user', 'action', 'model', 'object', 'date_from', 'date_to')}
    if f['user']:
        entries = entries.filter(username__icontains=f['user'])
    if f['action']:
        entries = entries.filter(action=f['action'])
    if f['model']:
        entries = entries.filter(model=f['model'])
    if f['object']:
        entries = entries.filter(object_repr__icontains=f['object']) | entries.filter(object_id=f['object'])
    for key, bound in (('date_from', time.min), ('date_to', time.max)):
        try:
            day = datetime.strptime(f[key], '%Y-%m-%d').date()
        except ValueError:
            continue
        moment = timezone.make_aware(datetime.combine(day, bound))
        entries = entries.filter(timestamp__gte=moment) if key == 'date_from' else entries.filter(timestamp__lte=moment)
    page = Paginator(entries, 100).get_page(request.GET.get('page'))
    query = request.GET.copy()
    query.pop('page', None)
    return render(request, 'src/audit.html', {
        'page': page, 'filters': f, 'query': query.urlencode(),
        'actions': AuditLog.ACTION_CHOICES,
        'models': ['Student', 'StudentResult', 'StudentBehaviouralAssessment', 'StudentClass', 'Section',
                   'Session', 'Term', 'Subject', 'User', 'UserProfile', 'ReviewDecision'],
    })


def deleted_groups(user):
    """Soft-deleted rows in the user's scope, grouped by the delete that removed them."""
    ids = set(scoped_classes(user, include_archived=True).values_list('id', flat=True))
    groups = []
    for kind, (model, label) in BIN_MODELS.items():
        rows = (model.all_objects.filter(deleted_at__isnull=False, student_class_id__in=ids)
                .select_related('deleted_by', 'student_class').order_by('-deleted_at'))
        if model is not Student:
            rows = rows.select_related('student', 'session', 'term')
        if model is StudentResult:
            rows = rows.select_related('subject')
        buckets = defaultdict(list)
        for row in rows:
            buckets[row.delete_batch or f'single-{row.id}'].append(row)
        for key, members in buckets.items():
            first = members[0]
            if model is Student:
                what = ', '.join(s.student_name for s in members[:5])
            else:
                subjects = sorted({str(r.subject) for r in members}) if model is StudentResult else []
                what = (f'{first.student_class.name} – {first.session} {first.term}'
                        + (f' – {", ".join(subjects[:3])}{"…" if len(subjects) > 3 else ""}' if subjects else '')
                        + (f' – {first.student}' if len(members) == 1 else ''))
            groups.append({'kind': kind, 'label': label, 'key': key, 'count': len(members), 'what': what,
                           'deleted_at': first.deleted_at, 'deleted_by': first.deleted_by,
                           'reason': first.delete_reason})
    groups.sort(key=lambda g: g['deleted_at'], reverse=True)
    return groups


def _conflicts(model, rows):
    """Active rows that a restore would duplicate."""
    clashes = []
    for row in rows:
        if model is StudentResult:
            clash = StudentResult.objects.filter(student_id=row.student_id, session_id=row.session_id,
                                                 term_id=row.term_id, subject_id=row.subject_id).first()
        elif model is StudentBehaviouralAssessment:
            clash = StudentBehaviouralAssessment.objects.filter(student_id=row.student_id, session_id=row.session_id,
                                                                term_id=row.term_id).first()
        else:
            clash = None
        if clash:
            clashes.append(f'{row.student}: an active record already exists ({clash})')
    return clashes


@require('delete_records')
def recycle_bin(request):
    if request.method == 'POST':
        kind = request.POST.get('kind')
        key = request.POST.get('key', '')
        if kind not in BIN_MODELS:
            messages.error(request, 'Unknown record type.')
            return redirect('recycle_bin')
        model, label = BIN_MODELS[kind]
        ids = set(scoped_classes(request.user, include_archived=True).values_list('id', flat=True))
        rows = model.all_objects.filter(deleted_at__isnull=False, student_class_id__in=ids)
        if key.startswith('single-'):
            rows = rows.filter(pk=key[len('single-'):] or 0)
        else:
            rows = rows.filter(delete_batch=key)
        rows = list(rows)
        if not rows:
            messages.error(request, 'Nothing to restore (it may already have been restored).')
            return redirect('recycle_bin')
        if model is not Student:
            from src.signing import lock_message, locked_by
            lock = next((locked_by(r.student_class_id, r.session_id, r.term_id) for r in rows
                         if locked_by(r.student_class_id, r.session_id, r.term_id)), None)
            if lock:
                messages.error(request, lock_message(lock))
                return redirect('recycle_bin')
        clashes = _conflicts(model, rows)
        if clashes:
            messages.error(request, f'Not restored: {len(clashes)} record(s) would duplicate existing ones. '
                                    'Delete or correct those first.')
            for c in clashes[:10]:
                messages.error(request, c)
            return redirect('recycle_bin')
        with transaction.atomic():
            for row in rows:
                row.restore()
            if model is StudentResult:
                for group in {(r.student_class_id, r.subject_id, r.session_id, r.term_id) for r in rows}:
                    recompute_positions(*group)
            audit.log('bulk_restore', model=model.__name__, object_repr=f'{len(rows)} {label} restored',
                      section_id=rows[0].student_class.section_id if rows[0].student_class_id else None,
                      details={'ids': [r.id for r in rows], 'batch': key})
        messages.success(request, f'Restored {len(rows)} {label}.')
        return redirect('recycle_bin')
    return render(request, 'src/recycle_bin.html', {'groups': deleted_groups(request.user)})
