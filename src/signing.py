"""E-signatures on results.

Staff keep a signature on their profile (drawn on screen or uploaded). A class
teacher / form master signs a class's results for a term; the Head of Section
approves them, which locks the results. Each approved report card carries a
QR code linking to a public page that confirms what was approved.
"""
import base64
import binascii
import hashlib
import hmac
import secrets
from io import BytesIO

import segno
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from src import audit
from src.access import has_perm, profile, require, scoped_class
from src.models import (ResultApproval, Session, Student, StudentBehaviouralAssessment, StudentResult, Term,
                        UserProfile)

MAX_SIGNATURE_BYTES = 2 * 1024 * 1024


# ------------------------------------------------------------------ queries

def active_approvals(student_class, session, term):
    found = ResultApproval.objects.filter(student_class=student_class, session=session, term=term,
                                          revoked_at__isnull=True)
    return {a.kind: a for a in found}


def locked_by(class_id, session_id, term_id):
    """The Head of Section approval locking these results, if any."""
    return ResultApproval.objects.filter(student_class_id=class_id, session_id=session_id, term_id=term_id,
                                         kind=ResultApproval.HEAD, revoked_at__isnull=True).first()


def lock_message(approval):
    return (f'These results were approved by {approval.signer_name} on {approval.signed_at:%d-%m-%Y} and are '
            'locked. A Head of Section must reopen them on the Sign Results page before they can be changed.')


# ------------------------------------------------------------------ verification

def student_token(approval, student_id):
    message = f'{approval.code}:{student_id}'.encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()[:12]


def verification_for(request, approval, student):
    path = reverse('verify_result', args=[approval.code, student.id, student_token(approval, student.id)])
    url = request.build_absolute_uri(path)
    return {'url': url, 'qr': segno.make(url, error='m').svg_data_uri(scale=2, border=1)}


def verify_result(request, code, student_id, token):
    """Public page behind the QR code on approved report cards."""
    approval = ResultApproval.objects.filter(code=code, kind=ResultApproval.HEAD).select_related(
        'student_class', 'session', 'term').first()
    valid = approval is not None and hmac.compare_digest(token, student_token(approval, student_id))
    context = {'valid': valid}
    if valid:
        from src.reports import build_class_report
        approved = approval.snapshot.get('students', {}).get(str(student_id))
        current, _, _ = build_class_report(approval.student_class, approval.session, approval.term)
        now = next((r for s, r in current.items() if s.id == student_id), None)
        context.update(approval=approval, approved=approved,
                       unchanged=bool(approved and now and approved['total'] == now['total']
                                      and approved['subjects'] == len(now['subjects'])))
    return render(request, 'src/verify.html', context)


# ------------------------------------------------------------------ signatures

def clean_signature(raw):
    """Validate an uploaded or drawn image; return it as a compact PNG data URL."""
    if len(raw) > MAX_SIGNATURE_BYTES:
        raise ValueError('The signature image is too large (limit 2 MB).')
    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError('That file is not an image.')
    image = image.convert('RGBA')
    # Make a white or near-white background transparent so the signature
    # prints cleanly over the report card background.
    image.putdata([(r, g, b, 0) if r > 225 and g > 225 and b > 225 else (r, g, b, a)
                   for r, g, b, a in image.getdata()])
    if image.getbbox() is None or image.getextrema()[3][1] == 0:
        raise ValueError('The signature is empty.')
    image.thumbnail((600, 200))
    out = BytesIO()
    image.save(out, format='PNG', optimize=True)
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()


def decode_data_url(data_url):
    if not data_url.startswith('data:image/png;base64,'):
        raise ValueError('Draw your signature before saving.')
    try:
        return base64.b64decode(data_url.split(',', 1)[1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('The drawn signature could not be read.')


def my_signature(request):
    if not request.user.is_authenticated:
        return render(request, 'src/welcome.html')
    p, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'remove':
                p.signature = ''
            elif action == 'upload':
                upload = request.FILES.get('signature_file')
                if upload is None:
                    raise ValueError('Choose an image file.')
                p.signature = clean_signature(upload.read())
            else:
                p.signature = clean_signature(decode_data_url(request.POST.get('signature_data', '')))
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('my_signature')
        p.save()
        messages.success(request, 'Signature removed.' if action == 'remove' else 'Signature saved.')
        return redirect('my_signature')
    return render(request, 'src/my_signature.html', {'signature': p.signature})


# ------------------------------------------------------------------ signing

def readiness(student_class, session, term):
    """Things worth checking before signing (shown as warnings)."""
    results = StudentResult.objects.filter(student_class=student_class, session=session, term=term)
    with_results = set(results.values_list('student_id', flat=True))
    with_behaviour = set(StudentBehaviouralAssessment.objects.filter(
        student_class=student_class, session=session, term=term).values_list('student_id', flat=True))
    roster = set(Student.objects.filter(student_class=student_class, status=True).values_list('id', flat=True))
    duplicates = (results.values('student_id', 'subject_id').annotate(n=Count('id')).filter(n__gt=1).count())
    from src.scoring import CA1_MAX, CA2_MAX, EXAM_MAX
    out_of_range = results.filter(ca1__gt=CA1_MAX).count() + results.filter(ca2__gt=CA2_MAX).count() \
        + results.filter(exams__gt=EXAM_MAX).count()
    return {
        'students_with_results': len(with_results),
        'without_behaviour': len(with_results - with_behaviour),
        'roster_without_results': len(roster - with_results),
        'duplicates': duplicates,
        'out_of_range': out_of_range,
    }


def snapshot(student_class, session, term):
    from src.reports import build_class_report
    report, subjects, _ = build_class_report(student_class, session, term)
    return {'subjects': [s.subject_name for s in subjects],
            'students': {str(s.id): {'name': s.student_name, 'total': r['total'], 'average': r['average'],
                                     'position': r['c_pos'], 'subjects': len(r['subjects'])}
                         for s, r in report.items()}}


def _selection(request):
    session = Session.objects.filter(pk=request.POST.get('session') or 0).first()
    term = Term.objects.filter(pk=request.POST.get('term') or 0).first()
    student_class = scoped_class(request.user, request.POST.get('class'), include_archived=True)
    return student_class, session, term


@require('enter_results')
def sign_results(request):
    context = {'can_approve': has_perm(request.user, 'approve_results')}
    if request.method != 'POST':
        return render(request, 'src/sign_results.html', context)
    student_class, session, term = _selection(request)
    if session is None or term is None:
        messages.error(request, 'Choose a session, term and class.')
        return render(request, 'src/sign_results.html', context)

    action = request.POST.get('action', 'show')
    if action in (ResultApproval.TEACHER, ResultApproval.HEAD):
        _sign(request, action, student_class, session, term)
    elif action == 'revoke':
        _revoke(request, student_class, session, term)

    context.update(sel={'class': student_class, 'session': session, 'term': term},
                   approvals=active_approvals(student_class, session, term),
                   history=ResultApproval.objects.filter(student_class=student_class, session=session, term=term)
                   .select_related('revoked_by'),
                   checks=readiness(student_class, session, term),
                   my_signature=getattr(profile(request.user), 'signature', ''))
    return render(request, 'src/sign_results.html', context)


def _sign(request, kind, student_class, session, term):
    if kind == ResultApproval.HEAD and not has_perm(request.user, 'approve_results'):
        messages.error(request, 'Only a Head of Section or Administrator can approve results.')
        return
    p = profile(request.user)
    if p is not None and p.is_legacy_shared:
        messages.error(request, 'Shared accounts cannot sign results: an e-signature must identify one person. '
                                'Ask the administrator for a personal account.')
        return
    signature = getattr(p, 'signature', '')
    if not signature:
        messages.error(request, 'Add your signature on the My Signature page first.')
        return
    if not StudentResult.objects.filter(student_class=student_class, session=session, term=term).exists():
        messages.error(request, 'There are no results to sign for this class and term.')
        return
    if kind in active_approvals(student_class, session, term):
        messages.error(request, 'This has already been signed. Reopen it first to sign again.')
        return
    with transaction.atomic():
        approval = ResultApproval.objects.create(
            student_class=student_class, session=session, term=term, kind=kind, signed_by=request.user,
            signer_name=request.user.get_full_name() or request.user.username, signature=signature,
            code=secrets.token_urlsafe(12)[:16], snapshot=snapshot(student_class, session, term))
        audit.log('review', model='ResultApproval', object_id=approval.id, object_repr=str(approval),
                  section_id=student_class.section_id,
                  details={'kind': kind, 'class_id': student_class.id, 'session_id': session.id, 'term_id': term.id,
                           'students': len(approval.snapshot['students'])})
    messages.success(request, 'Results approved and locked.' if kind == ResultApproval.HEAD
                     else 'Signed as class teacher / form master.')


def _revoke(request, student_class, session, term):
    approval = ResultApproval.objects.filter(pk=request.POST.get('approval_id') or 0, student_class=student_class,
                                             session=session, term=term, revoked_at__isnull=True).first()
    reason = request.POST.get('reason', '').strip()
    if approval is None:
        messages.error(request, 'That sign-off was not found.')
        return
    allowed = has_perm(request.user, 'approve_results') or (
        approval.kind == ResultApproval.TEACHER and approval.signed_by_id == request.user.id)
    if not allowed:
        messages.error(request, 'Only a Head of Section or Administrator can reopen approved results.')
        return
    if not reason:
        messages.error(request, 'Give a reason for reopening.')
        return
    approval.revoked_at = timezone.now()
    approval.revoked_by = request.user
    approval.revoke_reason = reason[:300]
    approval.save()
    audit.log('review', model='ResultApproval', object_id=approval.id, object_repr=f'Reopened: {approval}',
              section_id=student_class.section_id, details={'reason': reason})
    messages.success(request, 'Sign-off withdrawn; the results can be changed again.')
