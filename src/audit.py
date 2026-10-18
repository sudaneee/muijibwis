"""Audit trail: who changed what, when, and from which address.

Single-row saves and deletes of the tracked models are recorded automatically
through model signals, including changes made in the Django admin. Bulk
operations (uploads, bulk deletes, promotions) also write one summary entry
that ties the individual rows together.
"""
import contextvars
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.fields.files import FieldFile
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from src import models as m

_current = contextvars.ContextVar('audit_request', default=None)

TRACKED = (m.Section, m.Subject, m.StudentClass, m.Session, m.Term, m.Student, m.StudentResult,
           m.StudentBehaviouralAssessment, m.signature, m.sets, m.UserProfile)
USER_FIELDS = ('username', 'first_name', 'last_name', 'email', 'is_active', 'is_staff', 'is_superuser')
# Derived or bookkeeping fields that would only add noise to the trail.
IGNORED_FIELDS = {'subject_position', 'last_login'}


class AuditMiddleware:
    """Makes the current user and IP address available to signal handlers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current.set(request)
        try:
            return self.get_response(request)
        finally:
            _current.reset(token)


def client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (forwarded.split(',')[0].strip() or request.META.get('REMOTE_ADDR')) or None


def _actor(request=None):
    request = request or _current.get()
    if request is None:
        return None, '', None
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return user, user.get_username(), client_ip(request)
    return None, '', client_ip(request)


def plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, FieldFile):
        return value.name or ''
    return value


def field_values(instance):
    if isinstance(instance, get_user_model()):
        return {f: plain(getattr(instance, f)) for f in USER_FIELDS}
    return {f.attname: plain(getattr(instance, f.attname)) for f in instance._meta.concrete_fields
            if f.attname not in IGNORED_FIELDS and not f.primary_key}


def section_of(instance):
    """The section a record belongs to, so Heads of Section can see its history."""
    try:
        if isinstance(instance, m.Section):
            return instance.pk
        if isinstance(instance, m.StudentClass):
            return instance.section_id
        if isinstance(instance, (m.StudentResult, m.StudentBehaviouralAssessment, m.Student)):
            cls = m.StudentClass.objects.filter(pk=instance.student_class_id).only('section_id').first()
            return cls.section_id if cls else None
        if isinstance(instance, m.UserProfile):
            return instance.section_id
    except Exception:
        return None
    return None


def log(action, *, model='', object_id='', object_repr='', section_id=None,
        changes=None, details=None, request=None, user=None):
    actor, username, ip = _actor(request)
    if user is not None:
        actor, username = user, user.get_username()
    return m.AuditLog.objects.create(
        user=actor, username=username, ip_address=ip, action=action, model=model,
        object_id=str(object_id or ''), object_repr=str(object_repr)[:300], section_id=section_id,
        changes=changes or {}, details=details or {})


def log_instance(action, instance, changes=None, details=None):
    return log(action, model=type(instance).__name__, object_id=instance.pk, object_repr=str(instance),
               section_id=section_of(instance), changes=changes, details=details)


# ------------------------------------------------------------------ signals

def _tracked(sender):
    return sender in TRACKED or sender is get_user_model()


@receiver(pre_save)
def remember_old_values(sender, instance, raw=False, **kwargs):
    if raw or not _tracked(sender) or instance.pk is None:
        return
    manager = getattr(sender, 'all_objects', sender._default_manager)
    old = manager.filter(pk=instance.pk).first()
    instance._audit_old = field_values(old) if old else None


@receiver(post_save)
def record_save(sender, instance, created, raw=False, **kwargs):
    if raw or not _tracked(sender):
        return
    new = field_values(instance)
    old = getattr(instance, '_audit_old', None)
    if created or old is None:
        log_instance('create', instance, changes={k: [None, v] for k, v in new.items()})
        return
    changes = {k: [old.get(k), v] for k, v in new.items() if old.get(k) != v}
    # set_password() keeps the raw password in _password until save() finishes.
    if isinstance(instance, get_user_model()) and getattr(instance, '_password', None) is not None:
        changes['password'] = ['(hidden)', '(changed)']
    if not changes:
        return
    action = 'update'
    if 'deleted_at' in changes:
        action = 'delete' if changes['deleted_at'][1] else 'restore'
    details = {}
    if action == 'delete':
        details = {'reason': new.get('delete_reason', ''), 'batch': new.get('delete_batch', '')}
    log_instance(action, instance, changes=changes, details=details)


@receiver(post_delete)
def record_hard_delete(sender, instance, **kwargs):
    if not _tracked(sender):
        return
    log_instance('purge', instance, changes={k: [v, None] for k, v in field_values(instance).items()})


@receiver(user_logged_in)
def record_login(sender, request, user, **kwargs):
    log('login', model='User', object_id=user.pk, object_repr=user.get_username(), request=request, user=user)


@receiver(user_logged_out)
def record_logout(sender, request, user, **kwargs):
    if user is not None:
        log('logout', model='User', object_id=user.pk, object_repr=user.get_username(), request=request, user=user)


@receiver(user_login_failed)
def record_login_failed(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get('username', '')
    log('login_failed', model='User', object_repr=username, request=request,
        details={'username': username})
