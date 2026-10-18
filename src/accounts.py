"""Personal staff accounts (Administrator only), password changes and resets."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy

from src import audit
from src.access import require
from src.models import Section, UserProfile

User = get_user_model()


def _form_values(request):
    return {k: request.POST.get(k, '').strip() for k in
            ('username', 'first_name', 'last_name', 'email', 'role', 'section', 'password')}


def _check_role_section(values):
    errors = []
    if values['role'] not in dict(UserProfile.ROLE_CHOICES):
        errors.append('Choose a role.')
    section = Section.objects.filter(pk=values['section'] or 0).first()
    if values['role'] != UserProfile.ADMIN and section is None:
        errors.append('Choose the section this person works in.')
    return errors, (None if values['role'] == UserProfile.ADMIN else section)


@require('manage_users')
def users(request):
    context = {'roles': UserProfile.ROLE_CHOICES, 'sections': Section.objects.order_by('section_name')}
    if request.method == 'POST':
        values = _form_values(request)
        errors, section = _check_role_section(values)
        if not values['username']:
            errors.append('Enter a username.')
        elif User.objects.filter(username__iexact=values['username']).exists():
            errors.append('That username is already taken.')
        if not values['first_name'] or not values['last_name']:
            errors.append("Enter the person's first and last name.")
        try:
            validate_password(values['password'], User(username=values['username'], first_name=values['first_name'],
                                                       last_name=values['last_name'], email=values['email']))
        except ValidationError as exc:
            errors.extend(exc.messages)
        if errors:
            for e in errors:
                messages.error(request, e)
            context['values'] = values
        else:
            with transaction.atomic():
                user = User.objects.create_user(values['username'], email=values['email'], password=values['password'],
                                                first_name=values['first_name'], last_name=values['last_name'])
                UserProfile.objects.create(user=user, role=values['role'], section=section, must_change_password=True)
            messages.success(request, f'Created {user.username}. They must choose a new password when they first log in.')
            return redirect('users')
    context['accounts'] = (User.objects.select_related('profile', 'profile__section')
                           .order_by('-is_active', 'profile__is_legacy_shared', 'username'))
    return render(request, 'src/users.html', context)


@require('manage_users')
def user_edit(request, user_id):
    account = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    profile, _ = UserProfile.objects.get_or_create(user=account)
    context = {'account': account, 'profile': profile, 'roles': UserProfile.ROLE_CHOICES,
               'sections': Section.objects.order_by('section_name')}
    if request.method == 'POST':
        if 'reset_password' in request.POST:
            password = request.POST.get('new_password', '')
            try:
                validate_password(password, account)
            except ValidationError as exc:
                for e in exc.messages:
                    messages.error(request, e)
                return render(request, 'src/user_edit.html', context)
            account.set_password(password)
            account.save()
            profile.must_change_password = True
            profile.save()
            messages.success(request, f'Password reset for {account.username}; they must change it at next login.')
            return redirect('user_edit', user_id=account.id)

        values = _form_values(request)
        errors, section = _check_role_section(values)
        active = bool(request.POST.get('is_active'))
        if account == request.user and (values['role'] != UserProfile.ADMIN or not active):
            errors.append('You cannot remove your own administrator access or deactivate yourself.')
        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, 'src/user_edit.html', context)
        with transaction.atomic():
            account.first_name, account.last_name, account.email = (
                values['first_name'], values['last_name'], values['email'])
            account.is_active = active
            account.save()
            profile.role, profile.section = values['role'], section
            profile.save()
        messages.success(request, f'Saved {account.username}.')
        return redirect('users')
    return render(request, 'src/user_edit.html', context)


def password_change(request):
    if not request.user.is_authenticated:
        return render(request, 'src/welcome.html')
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        UserProfile.objects.filter(user=user).update(must_change_password=False)
        messages.success(request, 'Your password has been changed.')
        return redirect('home')
    return render(request, 'src/password_change.html', {'form': form})


class ForcePasswordChangeMiddleware:
    """Send users with a temporary password to the change-password page."""
    ALLOWED = ('/password-change', '/password-reset', '/logout', '/static/', '/media/', '/verify/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not request.path.startswith(self.ALLOWED):
            profile = UserProfile.objects.filter(user=user).only('must_change_password').first()
            if profile is not None and profile.must_change_password:
                return redirect('password_change')
        return self.get_response(request)


# ------------------------------------------------------------------ forgotten password

class PasswordResetRequest(auth_views.PasswordResetView):
    """Email a reset link. Never reveals whether an address belongs to an account."""
    template_name = 'src/password_reset_form.html'
    email_template_name = 'src/password_reset_email.txt'
    subject_template_name = 'src/password_reset_subject.txt'
    success_url = reverse_lazy('password_reset_done')

    def dispatch(self, request, *args, **kwargs):
        if not settings.EMAIL_HOST:
            return render(request, 'src/password_reset_unavailable.html')
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        audit.log('password', model='User', object_repr=form.cleaned_data['email'],
                  details={'event': 'reset requested', 'email': form.cleaned_data['email']})
        return super().form_valid(form)


class PasswordResetConfirm(auth_views.PasswordResetConfirmView):
    template_name = 'src/password_reset_confirm.html'
    success_url = reverse_lazy('password_reset_complete')

    def form_valid(self, form):
        response = super().form_valid(form)
        UserProfile.objects.filter(user=form.user).update(must_change_password=False)
        audit.log('password', model='User', object_id=form.user.pk, object_repr=form.user.username,
                  user=form.user, details={'event': 'reset completed by email link'})
        return response
