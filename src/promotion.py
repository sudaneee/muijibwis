"""Promote a class: Head of Section (own section) or Administrator only.

Step 1 picks the session and the from/to classes, step 2 previews every
student's third-term average with a suggested decision that can be changed,
step 3 moves the ticked students. Each promotion is recorded in the audit
trail and can be reversed from the history on the same page.
"""
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render

from src import audit
from src.access import require, scoped_class, sees_all_sections, user_section
from src.models import AuditLog, Session, Student, StudentResult, Term

PASS_MARK = 40


def third_term():
    return Term.objects.filter(term_name__iexact='Third Term').first() or Term.objects.filter(pk=3).first()


def _choices(request):
    session = Session.objects.filter(pk=request.POST.get('session') or 0).first()
    source = scoped_class(request.user, request.POST.get('current_class'))
    target = scoped_class(request.user, request.POST.get('promotion_class'), include_archived=True)
    return session, source, target


def preview_rows(source, session, move_all):
    term = third_term()
    rows = []
    for student in Student.objects.filter(student_class=source, status=True).order_by('student_name'):
        totals = list(StudentResult.objects.filter(student=student, session=session, term=term)
                      .values_list('total', flat=True))
        average = round(sum(totals) / len(totals), 2) if totals else None
        promote = move_all or (average is not None and average >= PASS_MARK)
        rows.append({'student': student, 'subjects': len(totals), 'average': average, 'promote': promote})
    return rows


def history(user):
    entries = AuditLog.objects.filter(action='promote')
    if not sees_all_sections(user):
        entries = entries.filter(section=user_section(user))
    undone = set(AuditLog.objects.filter(action='promote_undo')
                 .values_list('details__original_id', flat=True))
    entries = list(entries[:30])
    for e in entries:
        e.undone = e.id in undone
    return entries


@require('promote')
def promote(request):
    context = {'pass_mark': PASS_MARK, 'history': history(request.user)}
    if request.method != 'POST':
        return render(request, 'src/promote.html', context)

    if 'undo' in request.POST:
        return _undo(request)

    session, source, target = _choices(request)
    if session is None:
        messages.error(request, 'Choose a session.')
        return render(request, 'src/promote.html', context)
    if source.id == target.id:
        messages.error(request, 'Choose a different class to promote into.')
        return render(request, 'src/promote.html', context)

    if 'preview' in request.POST:
        move_all = bool(request.POST.get('move_all'))
        context.update(rows=preview_rows(source, session, move_all), session=session, source=source,
                       target=target, move_all=move_all, term=third_term())
        return render(request, 'src/promote.html', context)

    if 'confirm' in request.POST:
        chosen = {int(i) for i in request.POST.getlist('student_id') if i.isdigit()}
        students = list(Student.objects.filter(pk__in=chosen, student_class=source, status=True))
        if not students:
            messages.error(request, 'No students were selected.')
            return redirect('promote')
        with transaction.atomic():
            for student in students:
                student.student_class = target
                student.save()
            entry = audit.log(
                'promote', model='StudentClass', object_id=source.id,
                object_repr=f'{len(students)} students: {source.name} → {target.name} ({session})',
                section_id=source.section_id,
                details={'from_class_id': source.id, 'to_class_id': target.id, 'session_id': session.id,
                         'student_ids': [s.id for s in students],
                         'not_promoted': sorted(set(Student.objects.filter(student_class=source, status=True)
                                                    .values_list('id', flat=True)))})
        messages.success(request, f'Promoted {len(students)} students from {source.name} to {target.name}. '
                                  f'This can be reversed below (entry #{entry.id}).')
        return redirect('promote')

    return render(request, 'src/promote.html', context)


def _undo(request):
    entry = AuditLog.objects.filter(pk=request.POST.get('undo') or 0, action='promote').first()
    if entry is None or AuditLog.objects.filter(action='promote_undo', details__original_id=entry.id).exists():
        messages.error(request, 'That promotion was not found or has already been reversed.')
        return redirect('promote')
    source = scoped_class(request.user, entry.details['from_class_id'])
    target = scoped_class(request.user, entry.details['to_class_id'], include_archived=True)
    students = list(Student.objects.filter(pk__in=entry.details['student_ids'], student_class=target))
    with transaction.atomic():
        for student in students:
            student.student_class = source
            student.save()
        audit.log('promote_undo', model='StudentClass', object_id=source.id,
                  object_repr=f'Reversed: {len(students)} students back to {source.name} from {target.name}',
                  section_id=source.section_id,
                  details={'original_id': entry.id, 'student_ids': [s.id for s in students]})
    skipped = len(entry.details['student_ids']) - len(students)
    note = f' {skipped} had already moved elsewhere and were left alone.' if skipped else ''
    messages.success(request, f'Moved {len(students)} students back to {source.name}.{note}')
    return redirect('promote')
