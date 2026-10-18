"""Data review: find problem records, let an Exams Officer fix them, undo fixes.

Nothing here deletes data without first storing the complete original row in
a ReviewDecision, and every decision can be undone from the review home page.
All lists and actions are limited to the classes in the user's section.
"""
import re
from collections import Counter, defaultdict
from io import BytesIO

from django.contrib import messages
from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from openpyxl import Workbook

from src import audit
from src.access import review_required, scoped_classes, sees_all_sections, user_section
from src.models import (ReviewDecision, Student, StudentBehaviouralAssessment,
                        StudentClass, StudentResult)
from src.signing import lock_message, locked_by
from src.scoring import (BEHAVIOUR_FIELDS, BEHAVIOUR_MAX, CA1_MAX, CA2_MAX, EXAM_MAX,
                         behaviour_errors, expected_positions, recompute_positions,
                         score_errors)

MODELS = {'result': StudentResult, 'behaviour': StudentBehaviouralAssessment, 'student': Student}


# ---------------------------------------------------------------- row storage

def row_data(obj):
    return {f.attname: getattr(obj, f.attname) for f in obj._meta.concrete_fields}


def snapshot(label, obj):
    return {'model': label, 'data': row_data(obj)}


def _columns(model, names):
    allowed = {f.attname: f.column for f in model._meta.concrete_fields}
    return [allowed[n] for n in names]  # KeyError for anything not a real field


def restore_row(entry):
    """Re-insert a stored row with its original id and exact original values.

    Raw SQL is used so values that were invalid in the original data (e.g. a
    blank behavioural score) come back exactly as they were.
    """
    model = MODELS[entry['model']]
    data = entry['data']
    if model.objects.filter(pk=data['id']).exists():
        return False
    names = list(data)
    cols = ', '.join(f'"{c}"' for c in _columns(model, names))
    marks = ', '.join(['%s'] * len(names))
    with connection.cursor() as cursor:
        cursor.execute(f'INSERT INTO "{model._meta.db_table}" ({cols}) VALUES ({marks})',
                       [data[n] for n in names])
    return True


def write_fields(model, pk, fields):
    sets = ', '.join(f'"{c}" = %s' for c in _columns(model, list(fields)))
    with connection.cursor() as cursor:
        cursor.execute(f'UPDATE "{model._meta.db_table}" SET {sets} WHERE id = %s',
                       [*fields.values(), pk])


def current_fields(model, pk, names):
    row = model.objects.filter(pk=pk).values(*names).first()
    return row


def record(request, kind, target, action, summary, before, after=None, note=''):
    decision = ReviewDecision.objects.create(
        kind=kind, target=target, action=action, summary=summary[:500],
        before=before, after=after or {}, note=note, decided_by=request.user)
    audit.log('review', model='ReviewDecision', object_id=decision.id, object_repr=summary,
              section_id=getattr(user_section(request.user), 'id', None),
              details={'kind': kind, 'action': action, 'target': target, 'note': note})
    return decision


# --------------------------------------------------------------------- scope

def lock_of(row):
    return locked_by(row.student_class_id, row.session_id, row.term_id)


def scope_ids(user):
    return set(scoped_classes(user, include_archived=True).values_list('id', flat=True))


def result_in_scope(result, ids):
    return result.student_class_id in ids or result.student.student_class_id in ids


# ------------------------------------------------------------ issue finders

def _majority_class(rows):
    counts = Counter(r.student_class_id for r in rows)
    ranked = counts.most_common(2)
    if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]


def duplicate_result_groups(ids):
    keys = (StudentResult.objects.values('student_id', 'session_id', 'term_id', 'subject_id')
            .annotate(n=Count('id')).filter(n__gt=1))
    students = {k['student_id'] for k in keys}
    rows = (StudentResult.objects.filter(student_id__in=students)
            .select_related('student', 'student__student_class', 'student_class__section',
                            'subject', 'session', 'term'))
    by_key, by_term = defaultdict(list), defaultdict(list)
    for r in rows:
        by_key[(r.student_id, r.session_id, r.term_id, r.subject_id)].append(r)
        by_term[(r.student_id, r.session_id, r.term_id)].append(r)
    groups = []
    for key, copies in by_key.items():
        if len(copies) < 2 or not any(result_in_scope(c, ids) for c in copies):
            continue
        majority = _majority_class(by_term[key[:3]])
        suggested = [c.id for c in copies if c.student_class_id == majority]
        first = copies[0]
        groups.append({
            'key': '-'.join(map(str, key)), 'student': first.student, 'session': first.session,
            'term': first.term, 'subject': first.subject,
            'copies': sorted(copies, key=lambda c: c.id),
            'suggested_id': suggested[0] if len(suggested) == 1 else None,
        })
    groups.sort(key=lambda g: (g['session'].session_name, g['term'].id,
                               g['student'].student_name.strip().lower(), g['subject'].subject_name))
    return groups


def duplicate_behaviour_groups(ids):
    keys = (StudentBehaviouralAssessment.objects.values('student_id', 'session_id', 'term_id')
            .annotate(n=Count('id')).filter(n__gt=1))
    students = {k['student_id'] for k in keys}
    rows = (StudentBehaviouralAssessment.objects.filter(student_id__in=students)
            .select_related('student', 'student__student_class', 'student_class__section',
                            'session', 'term'))
    results_by_term = defaultdict(list)
    for r in StudentResult.objects.filter(student_id__in=students).only(
            'student_id', 'session_id', 'term_id', 'student_class_id'):
        results_by_term[(r.student_id, r.session_id, r.term_id)].append(r)
    by_key = defaultdict(list)
    for r in rows:
        by_key[(r.student_id, r.session_id, r.term_id)].append(r)
    groups = []
    for key, copies in by_key.items():
        if len(copies) < 2 or not any(result_in_scope(c, ids) for c in copies):
            continue
        majority = _majority_class(results_by_term[key])
        suggested = [c.id for c in copies if c.student_class_id == majority]
        first = copies[0]
        groups.append({
            'key': '-'.join(map(str, key)), 'student': first.student, 'session': first.session,
            'term': first.term, 'copies': sorted(copies, key=lambda c: c.id),
            'suggested_id': suggested[0] if len(suggested) == 1 else None,
        })
    groups.sort(key=lambda g: (g['session'].session_name, g['term'].id,
                               g['student'].student_name.strip().lower()))
    return groups


def accepted_result_ids():
    """Results the Exams Officer accepted as recorded (old batches whose total
    is valid but was split differently between C.A and exams)."""
    accepted = set()
    for before in ReviewDecision.objects.filter(
            kind='result_score', action='accept', undone_at__isnull=True).values_list('before', flat=True):
        accepted.update(before.get('result_ids', []))
    return accepted


def acceptable(result):
    return all(v >= 0 for v in (result.ca1, result.ca2, result.exams)) and 0 <= result.total <= 100


def out_of_range_results(ids):
    bad = (Q(ca1__lt=0) | Q(ca1__gt=CA1_MAX) | Q(ca2__lt=0) | Q(ca2__gt=CA2_MAX)
           | Q(exams__lt=0) | Q(exams__gt=EXAM_MAX))
    rows = (StudentResult.objects.filter(bad, student_class_id__in=ids)
            .exclude(id__in=accepted_result_ids())
            .select_related('student', 'student_class__section', 'subject', 'session', 'term'))
    for r in rows:
        r.acceptable = acceptable(r)
    return sorted(rows, key=lambda r: (r.session.session_name, r.term_id,
                                       r.student_class.name, r.student.student_name.strip().lower()))


def bad_behaviour(ids):
    bad = Q()
    for field in BEHAVIOUR_FIELDS:
        # In SQLite a text value such as '' also compares greater than 5.
        bad |= Q(**{f'{field}__lt': 0}) | Q(**{f'{field}__gt': BEHAVIOUR_MAX})
    rows = (StudentBehaviouralAssessment.objects.filter(bad, student_class_id__in=ids)
            .select_related('student', 'student_class__section', 'session', 'term'))
    return sorted(rows, key=lambda r: (r.session.session_name, r.term_id,
                                       r.student_class.name, r.student.student_name.strip().lower()))


def normalise_name(name):
    return re.sub(r'\s+', ' ', (name or '')).strip().lower()


def dismissed_student_pairs():
    return set(ReviewDecision.objects.filter(
        kind='duplicate_student', action='dismiss', undone_at__isnull=True
    ).values_list('target', flat=True))


def pair_target(a, b):
    low, high = sorted((int(a), int(b)))
    return f'dup-student:{low}:{high}'


def duplicate_students(ids):
    students = Student.objects.filter(student_class_id__in=ids).select_related('student_class__section')
    buckets = defaultdict(list)
    for s in students:
        buckets[(s.student_class_id, normalise_name(s.student_name))].append(s)
    dismissed = dismissed_student_pairs()
    groups = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda s: s.id)
        pairs = {pair_target(a.id, b.id) for i, a in enumerate(members) for b in members[i + 1:]}
        if pairs <= dismissed:
            continue
        for s in members:
            s.result_count = StudentResult.objects.filter(student=s).count()
            s.behaviour_count = StudentBehaviouralAssessment.objects.filter(student=s).count()
        groups.append({'members': members, 'pair': f'{members[0].id}:{members[1].id}'})
    groups.sort(key=lambda g: (g['members'][0].student_class.name, normalise_name(g['members'][0].student_name)))
    return groups


def students_without_class(user):
    if not sees_all_sections(user):
        return []
    students = list(Student.objects.filter(student_class__isnull=True).order_by('student_name'))
    for s in students:
        last = (StudentResult.objects.filter(student=s).select_related('student_class__section', 'session')
                .order_by('-session__session_name', '-term_id').first())
        s.last_class = last.student_class if last else None
        s.last_session = last.session if last else None
        s.result_count = StudentResult.objects.filter(student=s).count()
    return students


def stale_positions(ids):
    rows = list(StudentResult.objects.filter(student_class_id__in=ids).only(
        'id', 'student_class_id', 'subject_id', 'session_id', 'term_id', 'total', 'subject_position'))
    groups = defaultdict(list)
    for r in rows:
        groups[(r.student_class_id, r.subject_id, r.session_id, r.term_id)].append(r)
    stale = {}
    for members in groups.values():
        expected = expected_positions(members)
        for r in members:
            if r.subject_position != expected[r.id]:
                stale[r.id] = (r.subject_position, expected[r.id])
    return stale


# ----------------------------------------------------------------- views

@review_required
def review_home(request):
    ids = scope_ids(request.user)
    counts = {
        'duplicate_results': len(duplicate_result_groups(ids)),
        'duplicate_behaviour': len(duplicate_behaviour_groups(ids)),
        'scores': len(out_of_range_results(ids)),
        'behaviour': len(bad_behaviour(ids)),
        'duplicate_students': len(duplicate_students(ids)),
        'no_class': len(students_without_class(request.user)),
        'positions': len(stale_positions(ids)),
    }
    decisions = ReviewDecision.objects.select_related('decided_by', 'undone_by')
    if not sees_all_sections(request.user):
        decisions = decisions.filter(decided_by__profile__section=user_section(request.user))
    return render(request, 'src/review/home.html', {
        'counts': counts, 'total': sum(counts.values()),
        'decisions': decisions[:100],
        'limits': {'ca1': CA1_MAX, 'ca2': CA2_MAX, 'exams': EXAM_MAX, 'behaviour': BEHAVIOUR_MAX},
    })


@review_required
def review_duplicates(request):
    ids = scope_ids(request.user)
    if request.method == 'POST':
        kind = request.POST.get('kind')
        finder = duplicate_result_groups if kind == 'result' else duplicate_behaviour_groups
        groups = {g['key']: g for g in finder(ids)}
        if 'apply_suggested' in request.POST:
            if not request.POST.get('confirm'):
                messages.error(request, 'Tick the confirmation box to apply all suggestions.')
                return redirect('review_duplicates')
            plan = [(g, g['suggested_id']) for g in groups.values() if g['suggested_id']]
        else:
            group = groups.get(request.POST.get('key'))
            try:
                keep_id = int(request.POST.get('keep_id', ''))
            except ValueError:
                keep_id = None
            if group is None or keep_id not in [c.id for c in group['copies']]:
                messages.error(request, 'That duplicate is no longer open, or no copy was chosen.')
                return redirect('review_duplicates')
            plan = [(group, keep_id)]
        locked = [g for g, _ in plan if any(lock_of(c) for c in g['copies'])]
        if locked:
            messages.error(request, f'{len(locked)} duplicate group(s) belong to approved results; '
                                    'a Head of Section must reopen them first. Nothing was changed.')
            return redirect('review_duplicates')
        note = request.POST.get('note', '').strip()
        with transaction.atomic():
            for group, keep_id in plan:
                _keep_one(request, kind, group, keep_id, note)
        messages.success(request, f'Resolved {len(plan)} duplicate group(s). Each can be undone from Data Review.')
        return redirect('review_duplicates')
    return render(request, 'src/review/duplicates.html', {
        'result_groups': duplicate_result_groups(ids),
        'behaviour_groups': duplicate_behaviour_groups(ids),
    })


def _keep_one(request, kind, group, keep_id, note):
    label = 'result' if kind == 'result' else 'behaviour'
    removed = [c for c in group['copies'] if c.id != keep_id]
    kept = next(c for c in group['copies'] if c.id == keep_id)
    before = {'rows': [snapshot(label, c) for c in removed]}
    for c in removed:
        c.delete()
    if label == 'result':
        for c in removed + [kept]:
            recompute_positions(c.student_class_id, c.subject_id, c.session_id, c.term_id)
        what = f"{group['subject']} "
    else:
        what = 'behavioural assessment '
    record(request, f'duplicate_{"result" if label == "result" else "behaviour"}',
           f'{label}-dup:{group["key"]}', 'remove',
           f"{group['student']}: kept {what}copy in {kept.student_class.name} "
           f"({group['session']} {group['term']}), removed {len(removed)} other(s)",
           before, {'kept_id': keep_id}, note)


@review_required
def review_scores(request):
    ids = scope_ids(request.user)
    if request.method == 'POST' and request.POST.get('action') == 'accept_batch':
        return _accept_batch(request, ids)
    if request.method == 'POST':
        result = (StudentResult.objects.select_related('student', 'student_class', 'subject')
                  .filter(pk=request.POST.get('result_id'), student_class_id__in=ids).first())
        if result is None:
            messages.error(request, 'That result was not found in your section.')
            return redirect('review_scores')
        if lock_of(result):
            messages.error(request, lock_message(lock_of(result)))
            return redirect('review_scores')
        note = request.POST.get('note', '').strip()
        label = f'{result.student} – {result.subject} ({result.student_class.name})'
        with transaction.atomic():
            if request.POST.get('action') == 'accept':
                if not acceptable(result) or result.id in accepted_result_ids():
                    messages.error(request, f'{label}: only scores with a total from 0 to 100 can be accepted as recorded.')
                    return redirect('review_scores')
                record(request, 'result_score', f'result:{result.id}', 'accept',
                       f'Accepted as recorded {label}: {result.ca1}/{result.ca2}/{result.exams} (total {result.total})',
                       {'result_ids': [result.id]}, note=note)
                messages.success(request, f'Accepted {label} as recorded.')
                return redirect('review_scores')
            if request.POST.get('action') == 'remove':
                before = {'rows': [snapshot('result', result)]}
                result_id = result.id
                result.delete()
                recompute_positions(result.student_class_id, result.subject_id, result.session_id, result.term_id)
                record(request, 'result_score', f'result:{result_id}', 'remove',
                       f'Removed result {label}', before, note=note)
                messages.success(request, f'Removed result for {label}.')
            else:
                ca1, ca2, exams = (request.POST.get(f, '').strip() for f in ('ca1', 'ca2', 'exams'))
                errors = score_errors(ca1, ca2, exams)
                if errors:
                    messages.error(request, f'{label}: ' + '; '.join(errors))
                    return redirect('review_scores')
                old = {'ca1': result.ca1, 'ca2': result.ca2, 'exams': result.exams, 'total': result.total}
                new = {'ca1': int(ca1), 'ca2': int(ca2), 'exams': int(exams)}
                new['total'] = sum(new.values())
                StudentResult.objects.filter(pk=result.pk).update(**new)
                recompute_positions(result.student_class_id, result.subject_id, result.session_id, result.term_id)
                record(request, 'result_score', f'result:{result.id}', 'edit',
                       f'Corrected {label}: {old["ca1"]}/{old["ca2"]}/{old["exams"]} → '
                       f'{new["ca1"]}/{new["ca2"]}/{new["exams"]}',
                       {'model': 'result', 'id': result.id, 'fields': old}, {'fields': new}, note)
                messages.success(request, f'Corrected scores for {label}.')
        return redirect('review_scores')
    rows = out_of_range_results(ids)
    return render(request, 'src/review/scores.html', {
        'rows': rows, 'batches': score_batches(rows),
        'limits': {'ca1': CA1_MAX, 'ca2': CA2_MAX, 'exams': EXAM_MAX},
    })


def score_batches(rows):
    """Group open score issues by class/session/term for batch acceptance."""
    batches = defaultdict(list)
    for r in rows:
        batches[(r.student_class_id, r.session_id, r.term_id)].append(r)
    return [{'class': rs[0].student_class, 'session': rs[0].session, 'term': rs[0].term,
             'count': len(rs), 'acceptable': sum(r.acceptable for r in rs)}
            for rs in batches.values() if len(rs) >= 3]


def _accept_batch(request, ids):
    try:
        class_id, session_id, term_id = (int(request.POST.get(k, '')) for k in ('class_id', 'session_id', 'term_id'))
    except ValueError:
        messages.error(request, 'Invalid batch.')
        return redirect('review_scores')
    if class_id not in ids:
        messages.error(request, 'That class is not in your section.')
        return redirect('review_scores')
    rows = [r for r in out_of_range_results({class_id})
            if r.session_id == session_id and r.term_id == term_id and r.acceptable]
    if not rows:
        messages.error(request, 'Nothing in that batch can be accepted.')
        return redirect('review_scores')
    first = rows[0]
    record(request, 'result_score', f'result-batch:{class_id}:{session_id}:{term_id}', 'accept',
           f'Accepted as recorded {len(rows)} results in {first.student_class.name} '
           f'({first.session} {first.term}) whose totals are 0-100',
           {'result_ids': [r.id for r in rows]}, note=request.POST.get('note', '').strip())
    messages.success(request, f'Accepted {len(rows)} results as recorded.')
    return redirect('review_scores')


@review_required
def review_behaviour(request):
    ids = scope_ids(request.user)
    if request.method == 'POST':
        row = (StudentBehaviouralAssessment.objects.select_related('student', 'student_class')
               .filter(pk=request.POST.get('row_id'), student_class_id__in=ids).first())
        if row is None:
            messages.error(request, 'That assessment was not found in your section.')
            return redirect('review_behaviour')
        if lock_of(row):
            messages.error(request, lock_message(lock_of(row)))
            return redirect('review_behaviour')
        note = request.POST.get('note', '').strip()
        label = f'{row.student} ({row.student_class.name}, {row.session} {row.term})'
        with transaction.atomic():
            if request.POST.get('action') == 'remove':
                before = {'rows': [snapshot('behaviour', row)]}
                row_id = row.id
                row.delete()
                record(request, 'behaviour_score', f'behaviour:{row_id}', 'remove',
                       f'Removed behavioural assessment for {label}', before, note=note)
                messages.success(request, f'Removed behavioural assessment for {label}.')
            else:
                values = {f: request.POST.get(f, '').strip() for f in BEHAVIOUR_FIELDS}
                errors = behaviour_errors(values)
                if errors:
                    messages.error(request, f'{label}: ' + '; '.join(errors))
                    return redirect('review_behaviour')
                old = {f: getattr(row, f) for f in BEHAVIOUR_FIELDS}
                new = {f: int(v) for f, v in values.items()}
                write_fields(StudentBehaviouralAssessment, row.id, new)
                record(request, 'behaviour_score', f'behaviour:{row.id}', 'edit',
                       f'Corrected behavioural scores for {label}',
                       {'model': 'behaviour', 'id': row.id, 'fields': old}, {'fields': new}, note)
                messages.success(request, f'Corrected behavioural scores for {label}.')
        return redirect('review_behaviour')
    return render(request, 'src/review/behaviour.html', {
        'rows': bad_behaviour(ids), 'fields': BEHAVIOUR_FIELDS, 'max': BEHAVIOUR_MAX,
    })


@review_required
def review_students(request):
    ids = scope_ids(request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        note = request.POST.get('note', '').strip()
        with transaction.atomic():
            if action == 'remove':
                _remove_empty_student(request, ids, note)
            elif action == 'dismiss':
                _dismiss_pair(request, ids, note)
            elif action == 'assign':
                _assign_class(request, note)
        return redirect('review_students')
    return render(request, 'src/review/students.html', {
        'duplicates': duplicate_students(ids),
        'no_class': students_without_class(request.user),
        'all_classes': scoped_classes(request.user, include_archived=True).order_by('section__section_name', 'name'),
    })


def _remove_empty_student(request, ids, note):
    student = Student.objects.filter(pk=request.POST.get('student_id'), student_class_id__in=ids).first()
    in_duplicate = student and any(student in g['members'] for g in duplicate_students(ids))
    if not in_duplicate:
        messages.error(request, 'Only a record listed as a duplicate can be removed here.')
        return
    if (StudentResult.objects.filter(student=student).exists()
            or StudentBehaviouralAssessment.objects.filter(student=student).exists()):
        messages.error(request, f'{student} has results or assessments and cannot be removed.')
        return
    before = {'rows': [snapshot('student', student)]}
    student_id = student.id
    student.delete()
    record(request, 'duplicate_student', f'student:{student_id}', 'remove',
           f'Removed empty duplicate record "{student.student_name}" (id {student_id})', before, note=note)
    messages.success(request, f'Removed empty duplicate record for {student}.')


def _dismiss_pair(request, ids, note):
    try:
        a, b = (int(x) for x in request.POST.get('pair', '').split(':'))
    except ValueError:
        messages.error(request, 'Invalid selection.')
        return
    students = list(Student.objects.filter(pk__in=[a, b], student_class_id__in=ids))
    if len(students) != 2:
        messages.error(request, 'Those students were not found in your section.')
        return
    record(request, 'duplicate_student', pair_target(a, b), 'dismiss',
           f'Marked "{students[0].student_name}" ({a}) and ({b}) as different students', {}, note=note)
    messages.success(request, 'Marked as different students.')


def _assign_class(request, note):
    if not sees_all_sections(request.user):
        messages.error(request, 'Only administrators can assign students without a class.')
        return
    student = Student.objects.filter(pk=request.POST.get('student_id'), student_class__isnull=True).first()
    new_class = StudentClass.objects.filter(pk=request.POST.get('class_id')).first()
    if student is None or new_class is None:
        messages.error(request, 'Choose a student without a class and a class to assign.')
        return
    Student.objects.filter(pk=student.pk).update(student_class=new_class)
    record(request, 'student_class', f'student:{student.id}', 'edit',
           f'Assigned {student} to {new_class}',
           {'model': 'student', 'id': student.id, 'fields': {'student_class_id': None}},
           {'fields': {'student_class_id': new_class.id}}, note)
    messages.success(request, f'Assigned {student} to {new_class}.')


@review_required
@require_POST
def review_positions(request):
    ids = scope_ids(request.user)
    stale = stale_positions(ids)
    from src.views import locked_groups
    locked = locked_groups()
    if locked:
        groups = {r.id: (r.student_class_id, r.session_id, r.term_id) for r in StudentResult.objects.filter(
            pk__in=stale.keys()).only('id', 'student_class_id', 'session_id', 'term_id')}
        stale = {k: v for k, v in stale.items() if groups.get(k) not in locked}
    if not stale:
        messages.info(request, 'All subject positions are already correct.')
        return redirect('review_home')
    with transaction.atomic():
        rows = StudentResult.objects.filter(pk__in=stale.keys()).only('id', 'subject_position')
        for r in rows:
            r.subject_position = stale[r.id][1]
        StudentResult.objects.bulk_update(rows, ['subject_position'], batch_size=500)
        record(request, 'positions', f'positions:{timezone.now():%Y%m%d%H%M%S}', 'recalculate',
               f'Recalculated {len(stale)} subject positions',
               {'positions': {str(k): v[0] for k, v in stale.items()}},
               {'positions': {str(k): v[1] for k, v in stale.items()}})
    messages.success(request, f'Recalculated {len(stale)} subject positions.')
    return redirect('review_home')


@review_required
@require_POST
def review_undo(request, decision_id):
    decision = ReviewDecision.objects.select_related('decided_by').filter(pk=decision_id).first()
    allowed = decision and (sees_all_sections(request.user) or (
        decision.decided_by and user_section(decision.decided_by) == user_section(request.user)))
    if not allowed or decision.undone_at or decision.kind == 'class_section':
        messages.error(request, 'That decision cannot be undone here.')
        return redirect('review_home')
    with transaction.atomic():
        ok, message = _undo(decision)
        if ok:
            decision.undone_by = request.user
            decision.undone_at = timezone.now()
            decision.save(update_fields=['undone_by', 'undone_at'])
            audit.log('review', model='ReviewDecision', object_id=decision.id,
                      object_repr=f'Undone: {decision.summary}',
                      section_id=getattr(user_section(request.user), 'id', None),
                      details={'undo_of': decision.id})
    (messages.success if ok else messages.error)(request, message)
    return redirect('review_home')


def _undo(decision):
    if decision.action == 'dismiss':
        return True, 'The pair will appear in the duplicate list again.'
    if decision.action == 'accept':
        return True, 'The result(s) are back in the review list.'
    if decision.action == 'remove':
        restored = 0
        for entry in decision.before['rows']:
            restored += restore_row(entry)
            if entry['model'] == 'result':
                d = entry['data']
                recompute_positions(d['student_class_id'], d['subject_id'], d['session_id'], d['term_id'])
        return True, f'Restored {restored} record(s).'
    if decision.action == 'edit':
        model = MODELS[decision.before['model']]
        pk = decision.before['id']
        current = current_fields(model, pk, list(decision.after['fields']))
        if current != decision.after['fields']:
            return False, 'The record has changed since this decision, so it was not undone.'
        write_fields(model, pk, decision.before['fields'])
        if model is StudentResult:
            r = StudentResult.objects.get(pk=pk)
            recompute_positions(r.student_class_id, r.subject_id, r.session_id, r.term_id)
        return True, 'Restored the original values.'
    if decision.action == 'recalculate':
        old, new = decision.before['positions'], decision.after['positions']
        rows = list(StudentResult.objects.filter(pk__in=[int(k) for k in old]).only('id', 'subject_position'))
        reverted = [r for r in rows if r.subject_position == new[str(r.id)]]
        for r in reverted:
            r.subject_position = old[str(r.id)]
        StudentResult.objects.bulk_update(reverted, ['subject_position'], batch_size=500)
        return True, f'Restored {len(reverted)} previous positions.'
    return False, 'Unknown decision type.'


@review_required
def review_export(request):
    ids = scope_ids(request.user)
    book = Workbook()
    book.remove(book.active)

    def sheet(title, header, rows):
        ws = book.create_sheet(title)
        ws.append(header)
        for row in rows:
            ws.append(row)

    sheet('Duplicate results',
          ['Group', 'Result ID', 'Student ID', 'Student', 'Session', 'Term', 'Subject', 'Filed under class',
           'Section', 'CA1', 'CA2', 'Exams', 'Total', 'Suggested keep'],
          [[g['key'], c.id, c.student_id, c.student.student_name, str(g['session']), str(g['term']),
            str(g['subject']), c.student_class.name, str(c.student_class.section), c.ca1, c.ca2, c.exams,
            c.total, 'yes' if c.id == g['suggested_id'] else '']
           for g in duplicate_result_groups(ids) for c in g['copies']])
    sheet('Duplicate behaviour',
          ['Group', 'Assessment ID', 'Student ID', 'Student', 'Session', 'Term', 'Filed under class',
           *BEHAVIOUR_FIELDS, 'Suggested keep'],
          [[g['key'], c.id, c.student_id, c.student.student_name, str(g['session']), str(g['term']),
            c.student_class.name, *[getattr(c, f) for f in BEHAVIOUR_FIELDS],
            'yes' if c.id == g['suggested_id'] else '']
           for g in duplicate_behaviour_groups(ids) for c in g['copies']])
    sheet('Scores out of range',
          ['Result ID', 'Student ID', 'Student', 'Class', 'Session', 'Term', 'Subject', 'CA1', 'CA2', 'Exams', 'Total'],
          [[r.id, r.student_id, r.student.student_name, r.student_class.name, str(r.session), str(r.term),
            str(r.subject), r.ca1, r.ca2, r.exams, r.total] for r in out_of_range_results(ids)])
    sheet('Behaviour out of range',
          ['Assessment ID', 'Student ID', 'Student', 'Class', 'Session', 'Term', *BEHAVIOUR_FIELDS],
          [[r.id, r.student_id, r.student.student_name, r.student_class.name, str(r.session), str(r.term),
            *[getattr(r, f) for f in BEHAVIOUR_FIELDS]] for r in bad_behaviour(ids)])
    sheet('Duplicate students',
          ['Student ID', 'Name', 'Class', 'Results', 'Assessments'],
          [[s.id, s.student_name, s.student_class.name, s.result_count, s.behaviour_count]
           for g in duplicate_students(ids) for s in g['members']])
    sheet('Students without class',
          ['Student ID', 'Name', 'Results', 'Last class', 'Last session'],
          [[s.id, s.student_name, s.result_count, str(s.last_class or ''), str(s.last_session or '')]
           for s in students_without_class(request.user)])

    buffer = BytesIO()
    book.save(buffer)
    response = HttpResponse(buffer.getvalue(),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="data-review-{timezone.now():%Y-%m-%d}.xlsx"'
    return response
