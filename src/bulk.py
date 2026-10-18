"""Excel workflows with a preview step: bulk student upload and bulk result update.

Both follow download template -> upload -> check the preview -> confirm. The
checked rows wait in the user's session between preview and confirm, and are
re-checked against the database before anything is written.
"""
import re
import uuid

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from src import audit
from src.access import require, scoped_class
from src.models import CATEGORY, Student, StudentResult, Subject
from src.scoring import _as_int, recompute_positions, score_errors
from src.views import read_selection, read_sheet, refuse_if_locked, save_virtual_workbook, selection_label

GENDERS = {'m': 'Male', 'male': 'Male', 'f': 'Female', 'female': 'Female'}
CATEGORIES = {c.lower(): c for c, _ in CATEGORY}
LOCKED_FILL = PatternFill('solid', fgColor='EEEEEE')


def name_key(name):
    return re.sub(r'\s+', ' ', str(name or '')).strip().lower()


def xlsx_response(book, filename):
    response = HttpResponse(save_virtual_workbook(book),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ------------------------------------------------------------------ students

@require('manage_students')
def student_upload(request):
    template = 'src/stdcreate.html'
    if request.method != 'POST':
        return render(request, template, {})
    action = request.POST.get('action', 'preview')

    if action == 'template':
        book = Workbook()
        sheet = book.active
        sheet.title = 'students'
        sheet.append(['No', 'Name', 'Gender (Male/Female)', 'Category'])
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        gender = DataValidation(type='list', formula1='"Male,Female"', allow_blank=True)
        category = DataValidation(type='list', formula1='"' + ','.join(CATEGORIES.values()) + '"', allow_blank=True)
        sheet.add_data_validation(gender)
        sheet.add_data_validation(category)
        gender.add('C2:C500')
        category.add('D2:D500')
        sheet.column_dimensions['B'].width = 40
        sheet.column_dimensions['C'].width = 20
        sheet.column_dimensions['D'].width = 16
        return xlsx_response(book, 'students-template.xlsx')

    if action == 'confirm':
        return _confirm_students(request, template)

    student_class = scoped_class(request.user, request.POST.get('class'))
    rows, error = read_sheet(request, 'students', (1, 2, 3))
    if error or not rows:
        messages.error(request, error or 'The sheet has no student names.')
        return render(request, template, {})
    existing = {name_key(s.student_name) for s in Student.objects.filter(student_class=student_class)}
    seen, preview, errors = set(), [], []
    for label, (name, gender, category) in rows:
        name = re.sub(r'\s+', ' ', str(name or '')).strip()
        row = {'label': label, 'name': name, 'gender': None, 'category': 'Normal', 'warnings': [], 'include': True}
        if not name:
            errors.append(f'{label}: the name is empty')
            continue
        if gender not in (None, ''):
            row['gender'] = GENDERS.get(str(gender).strip().lower())
            if row['gender'] is None:
                errors.append(f'{label}: gender "{gender}" should be Male or Female')
        if category not in (None, ''):
            row['category'] = CATEGORIES.get(str(category).strip().lower())
            if row['category'] is None:
                errors.append(f'{label}: category "{category}" should be one of {", ".join(CATEGORIES.values())}')
        if name_key(name) in existing:
            row['warnings'].append(f'already in {student_class.name}')
            row['include'] = False
        if name_key(name) in seen:
            row['warnings'].append('appears twice in this sheet')
            row['include'] = False
        seen.add(name_key(name))
        preview.append(row)
    if errors:
        from src.views import report_errors
        report_errors(request, errors)
        return render(request, template, {})
    token = uuid.uuid4().hex
    request.session['student_upload'] = {'token': token, 'class_id': student_class.id, 'rows': preview}
    return render(request, template, {'preview': preview, 'token': token, 'student_class': student_class})


def _confirm_students(request, template):
    pending = request.session.get('student_upload')
    if not pending or pending['token'] != request.POST.get('token'):
        messages.error(request, 'This upload has expired. Upload the file again.')
        return render(request, template, {})
    student_class = scoped_class(request.user, pending['class_id'])
    chosen = {int(i) for i in request.POST.getlist('include') if i.isdigit()}
    rows = [r for n, r in enumerate(pending['rows']) if n in chosen]
    if not rows:
        messages.error(request, 'No students were ticked.')
        return render(request, template, {})
    created = []
    with transaction.atomic():
        for r in rows:
            student = Student.objects.create(student_name=r['name'], student_class=student_class, status=True,
                                             gender=r['gender'], category=r['category'])
            created.append(student.id)
        audit.log('upload', model='Student', object_repr=f'{len(created)} students added to {student_class.name}',
                  section_id=student_class.section_id,
                  details={'source': 'excel', 'class_id': student_class.id, 'student_ids': created})
    del request.session['student_upload']
    messages.success(request, f'Added {len(created)} students to {student_class.name}.')
    return render(request, template, {})


# ------------------------------------------------------------------ result update

@require('enter_results')
def result_excel_update(request):
    template = 'src/excel_update.html'
    context = {'subjects': Subject.objects.all()}
    if request.method != 'POST':
        return render(request, template, context)
    action = request.POST.get('action', 'preview')
    if action == 'confirm':
        return _apply_update(request, template, context)

    sel, error = read_selection(request)
    if error:
        messages.error(request, error)
        return render(request, template, context)
    results = sorted(StudentResult.objects.filter(session=sel['session'], term=sel['term'],
                                                  student_class=sel['class'], subject=sel['subject'])
                     .select_related('student'), key=lambda r: (name_key(r.student.student_name), r.id))
    if not results:
        messages.error(request, f'No results found for {selection_label(sel)}.')
        return render(request, template, context)

    if action == 'download':
        book = Workbook()
        sheet = book.active
        sheet.title = 'update'
        sheet.append(['Result ID', 'Student ID', 'Name', '1st C.A', '2nd C.A', 'Exams'])
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for r in results:
            sheet.append([r.id, r.student_id, r.student.student_name, r.ca1, r.ca2, r.exams])
        for row in sheet.iter_rows(min_row=2, max_col=3):
            for cell in row:
                cell.fill = LOCKED_FILL
        sheet.column_dimensions['C'].width = 40
        sheet['H1'] = f'{selection_label(sel)} — change only the score columns (D-F).'
        name = re.sub(r'[^A-Za-z0-9]+', '-', selection_label(sel)).strip('-')
        return xlsx_response(book, f'update-{name}.xlsx')

    if refuse_if_locked(request, sel):
        return render(request, template, context)
    rows, error = read_sheet(request, 'update', (0, 1, 3, 4, 5))
    if error or not rows:
        messages.error(request, error or 'The sheet has no rows.')
        return render(request, template, context)
    by_id = {r.id: r for r in results}
    errors, changes, seen = [], [], set()
    for label, (rid, sid, ca1, ca2, exams) in rows:
        result = by_id.get(_as_int(rid))
        if result is None or result.student_id != _as_int(sid):
            errors.append(f'{label}: result {rid} does not belong to {selection_label(sel)} '
                          '(download a fresh sheet for this class and subject)')
            continue
        if result.id in seen:
            errors.append(f'{label}: result {rid} appears more than once')
            continue
        seen.add(result.id)
        old = [result.ca1, result.ca2, result.exams]
        if [_as_int(ca1), _as_int(ca2), _as_int(exams)] == old:
            continue   # unchanged rows are left alone, even if Data Review has flagged them
        problems = score_errors(ca1, ca2, exams)
        if problems:
            errors.extend(f'{label} ({result.student}): {p}' for p in problems)
            continue
        changes.append({'id': result.id, 'name': result.student.student_name, 'old': old,
                        'new': [int(ca1), int(ca2), int(exams)]})
    if errors:
        from src.views import report_errors
        report_errors(request, errors)
        return render(request, template, context)
    if not changes:
        messages.info(request, 'The sheet matches the current scores; nothing to change.')
        return render(request, template, context)
    token = uuid.uuid4().hex
    request.session['result_update'] = {
        'token': token, 'class_id': sel['class'].id, 'session_id': sel['session'].id, 'term_id': sel['term'].id,
        'subject_id': sel['subject'].id, 'changes': changes}
    context.update(preview=changes, token=token, label=selection_label(sel))
    return render(request, template, context)


def _apply_update(request, template, context):
    pending = request.session.get('result_update')
    if not pending or pending['token'] != request.POST.get('token'):
        messages.error(request, 'This upload has expired. Upload the file again.')
        return render(request, template, context)
    student_class = scoped_class(request.user, pending['class_id'])
    from src.signing import lock_message, locked_by
    lock = locked_by(student_class.id, pending['session_id'], pending['term_id'])
    if lock:
        messages.error(request, lock_message(lock))
        return render(request, template, context)
    results = {r.id: r for r in StudentResult.objects.filter(
        pk__in=[c['id'] for c in pending['changes']], student_class=student_class,
        session_id=pending['session_id'], term_id=pending['term_id'], subject_id=pending['subject_id'])}
    stale = [c['name'] for c in pending['changes']
             if c['id'] not in results or [results[c['id']].ca1, results[c['id']].ca2, results[c['id']].exams] != c['old']]
    if stale:
        messages.error(request, f'Nothing was saved: {len(stale)} result(s) were changed by someone else after you '
                                f'uploaded ({", ".join(stale[:5])}). Download a fresh sheet and try again.')
        return render(request, template, context)
    with transaction.atomic():
        for c in pending['changes']:
            result = results[c['id']]
            result.ca1, result.ca2, result.exams = c['new']
            result.total = sum(c['new'])
            result.save()
        recompute_positions(student_class.id, pending['subject_id'], pending['session_id'], pending['term_id'])
        audit.log('upload', model='StudentResult', object_repr=f'{len(results)} results updated from Excel',
                  section_id=student_class.section_id,
                  details={'source': 'excel_update', 'class_id': student_class.id, 'session_id': pending['session_id'],
                           'term_id': pending['term_id'], 'subject_id': pending['subject_id'],
                           'result_ids': list(results)})
    del request.session['result_update']
    messages.success(request, f'Updated {len(results)} results.')
    return render(request, template, context)
