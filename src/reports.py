"""Report views: class report cards, broadsheet, single result, subject analysis.

All class reports are built by `build_class_report`, so report cards, the
broadsheet, the public result checker and e-signature snapshots agree. The
averages, class positions and highest average use the same formulas as the
original system. Students are listed alphabetically and each student's
behavioural assessment is matched by student (the original paired them by
list position, which could print another student's assessment).
"""
import datetime
import re

from django.contrib import messages
from django.shortcuts import render

from src.access import require, scoped_class
from src.models import (Session, StudentBehaviouralAssessment, StudentClass, StudentResult, Subject, Term, sets)
from src.signing import active_approvals, verification_for


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def name_key(student):
    return re.sub(r'\s+', ' ', student.student_name or '').strip().lower()


def build_class_report(student_class, session, term):
    """Per-student totals, averages, positions and behaviour for one class/term.

    Returns (students, subjects): `students` is an ordered dict keyed by
    student (alphabetical), `subjects` the subject columns in first-entered order.
    """
    results = (StudentResult.objects.filter(session=session, term=term, student_class=student_class)
               .select_related('student', 'subject', 'session', 'term', 'student_class').order_by('id'))
    by_student = {}
    subjects = []
    for r in results:
        by_student.setdefault(r.student, []).append(r)
        if r.subject not in subjects:
            subjects.append(r.subject)
    behaviour = {}
    for b in (StudentBehaviouralAssessment.objects.filter(session=session, term=term, student_class=student_class)
              .order_by('id')):
        behaviour.setdefault(b.student_id, b)   # first one if a duplicate slipped in

    report = {}
    for student in sorted(by_student, key=lambda s: (name_key(s), s.id)):
        rows = by_student[student]
        total = sum(r.total for r in rows)
        by_subject = {r.subject_id: r for r in rows}
        report[student] = {
            'student': student,
            'subjects': rows,
            'cells': [by_subject.get(s.id) for s in subjects],
            'total': total,
            'overal_total': total,
            'average': round(total / len(rows), 2),
            'terms': rows[0].term,
            'sesss': rows[0].session,
            'classs': rows[0].student_class,
            'bhv': behaviour.get(student.id),
        }
    averages = [r['average'] for r in report.values()]
    for r in report.values():
        r['position'] = 1 + sum(a > r['average'] for a in averages)
        r['c_pos'] = ordinal(r['position'])
    return report, subjects, averages


def card_context(request, student_class, session, term, student_ids=None):
    report, subjects, averages = build_class_report(student_class, session, term)
    approvals = active_approvals(student_class, session, term)
    head = approvals.get('head')
    for r in report.values():
        r['verify'] = verification_for(request, head, r['student']) if head else None
    if student_ids is not None:
        report = {s: r for s, r in report.items() if s.id in student_ids}
    section = student_class.section
    return {
        'date': datetime.datetime.now(),
        'f_classs': str(student_class),
        'section': section,
        'results': report,
        'some_images': sets.objects.first(),
        'p_term': term.term_name,
        'allAvr': averages,
        'noInClass': len(averages),
        'classHighestAverage': max(averages) if averages else '',
        'approvals': approvals,
    }


def card_template(student_class):
    section = student_class.section
    return 'src/single_result_view_ie.html' if section and 'IE' in section.section_name else 'src/single_result_view.html'


def _class_term(request):
    """Selected class (in scope, archived allowed), session and term, or None with a message."""
    if not all(request.POST.get(k, '').isdigit() for k in ('session', 'term', 'class')):
        messages.error(request, 'Choose a session, term and class.')
        return None
    student_class = scoped_class(request.user, request.POST['class'], include_archived=True)
    session = Session.objects.filter(pk=request.POST['session']).first()
    term = Term.objects.filter(pk=request.POST['term']).first()
    if session is None or term is None:
        messages.error(request, 'Choose a session, term and class.')
        return None
    if not StudentResult.objects.filter(session=session, term=term, student_class=student_class).exists():
        messages.error(request, 'No results have been entered for that class, session and term.')
        return None
    return student_class, session, term


@require('view_reports')
def result_view(request):
    if request.method == 'POST' and 'form1' in request.POST:
        chosen = _class_term(request)
        if chosen is None:
            return render(request, 'src/result_view.html', {})
        student_class, session, term = chosen
        return render(request, card_template(student_class), card_context(request, student_class, session, term))
    return render(request, 'src/result_view.html', {})


def single_result_view(request):
    """Public result checker: session, term and registration number."""
    if request.method == 'POST' and 'form1' in request.POST:
        session = request.POST.get('session', '')
        term = request.POST.get('term', '')
        final_student_id = request.POST.get('student_id', '').strip()[10:]
        if not (session.isdigit() and term.isdigit() and final_student_id.isdigit()):
            messages.error(request, 'Choose a session and term, and enter the full registration number.')
            return render(request, 'src/single_result_view1.html')
        first = (StudentResult.objects.filter(session=session, term=term, student=final_student_id)
                 .select_related('student_class__section', 'session', 'term').order_by('id').first())
        if first is None:
            messages.error(request, 'No result was found for that registration number, session and term.')
            return render(request, 'src/single_result_view1.html')
        context = card_context(request, first.student_class, first.session, first.term,
                               student_ids={int(final_student_id)})
        return render(request, card_template(first.student_class), context)
    return render(request, 'src/single_result_view1.html')


@require('view_reports')
def class_summary(request):
    if request.method == 'POST' and 'form1' in request.POST:
        classs, term, session, subject = (request.POST.get(k, '') for k in ('class', 'term', 'session', 'subject'))
        if not all(v.isdigit() for v in (classs, term, session, subject)):
            messages.error(request, 'Choose a class, term, session and subject.')
            return render(request, 'src/class_summary.html', {'subjects': Subject.objects.all()})
        student_class = scoped_class(request.user, classs, include_archived=True)
        subject = Subject.objects.filter(pk=subject).first()
        result_summary = sorted(
            StudentResult.objects.filter(subject=subject, student_class=student_class, session=session, term=term)
            .select_related('student'), key=lambda r: (name_key(r.student), r.student_id))
        bands = {'a_count': 70, 'b_count': 60, 'c_count': 50, 'd_count': 45, 'e_count': 40}
        counts = dict.fromkeys(list(bands) + ['f_count'], 0)
        for r in result_summary:
            band = next((k for k, floor in bands.items() if r.total >= floor), 'f_count')
            counts[band] += 1
        context = dict(counts, summary=result_summary, total_students=len(result_summary),
                       subject=subject.subject_name if subject else '', classs=student_class.class_name)
        return render(request, 'src/summary_page.html', context)
    return render(request, 'src/class_summary.html', {'subjects': Subject.objects.all()})


@require('view_reports')
def class_result_summary(request):
    if request.method == 'POST' and 'form1' in request.POST:
        chosen = _class_term(request)
        if chosen is None:
            return render(request, 'src/result_view.html', {})
        student_class, session, term = chosen
        report, subjects, _ = build_class_report(student_class, session, term)
        senior = student_class.section is not None and student_class.section.section_name == 'SS'
        context = {
            'date': datetime.datetime.now(),
            'f_classs': str(student_class),
            'results': report,
            'some_images': sets.objects.first(),
            'p_term': term.term_name,
            'eachSubject': subjects,
            'session': session.session_name,
            'term': term.term_name,
            'class': student_class,
            'directorAcadamic': 'Director Acadamic & Examination',
            'pcHeadTeacher': 'Principal' if senior else 'Head Teacher',
            'approvals': active_approvals(student_class, session, term),
        }
        return render(request, 'src/class_result_summary.html', context)
    return render(request, 'src/result_view.html', {})
