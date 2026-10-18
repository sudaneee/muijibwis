from django.conf import settings
from django.db import models

# Every relation uses PROTECT: deleting a section, class, session, term,
# subject or student that still has records is refused instead of silently
# deleting or orphaning those records.

class ActiveManager(models.Manager):
    """Default manager: hides soft-deleted rows everywhere (views, reports, admin)."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteModel(models.Model):
    """Rows are never removed by the application; they are marked deleted with
    who, when and why, and can be restored from the recycle bin."""
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name='+')
    delete_reason = models.CharField(max_length=300, blank=True, default='')
    delete_batch = models.CharField(max_length=40, blank=True, default='', db_index=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def soft_delete(self, user, reason='', batch=''):
        from django.utils import timezone
        self.deleted_at = timezone.now()
        self.deleted_by = user
        self.delete_reason = reason[:300]
        self.delete_batch = batch
        self.save()

    def restore(self):
        self.deleted_at = None
        self.deleted_by = None
        self.delete_reason = ''
        self.delete_batch = ''
        self.save()


CATEGORY = (
    ('Normal', 'Normal'),
    ('Complement', 'Complement'),
    ('Schoolarship', 'Schoolarship'),
    ('Dependant', 'Dependant')
)

class Section(models.Model):
    section_name = models.CharField(max_length=100)

    def __str__(self):
        return self.section_name

class Subject(models.Model):
    subject_name = models.CharField(max_length=100)

    def __str__(self):
        return(self.subject_name)

class StudentClass(models.Model):
    name = models.CharField(max_length=100)
    section = models.ForeignKey(Section, null=True, on_delete=models.PROTECT)
    # Archived classes (past rosters, graduated sets) stay available for
    # viewing reports but are hidden from data-entry dropdowns.
    is_archived = models.BooleanField(default=False, db_default=False)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return f"{str(self.name)} -- {str(self.section)}"

    def class_name(self):
        return f"{str(self.name)} -- {str(self.section)}"

class Session(models.Model):
    session_name = models.CharField(max_length=100)

    def __str__(self):
        return(self.session_name)

class Term(models.Model):
    term_name = models.CharField(max_length=100)

    def __str__(self):
        return(self.term_name)





class Student(SoftDeleteModel):
    student_name = models.CharField(max_length=100)
    section = models.ForeignKey(Section, null=True, on_delete=models.PROTECT)
    student_class = models.ForeignKey(StudentClass, null=True, on_delete=models.PROTECT)
    status = models.BooleanField(null=True, default=True)
    gender = models.CharField(max_length=100, null=True)
    category = models.CharField(max_length=200, choices=CATEGORY, default='Normal', null=True)

    def __str__(self):
        return(self.student_name)


class StudentResult(SoftDeleteModel):
    student = models.ForeignKey(Student, on_delete=models.PROTECT)
    session = models.ForeignKey(Session, on_delete=models.PROTECT)
    term = models.ForeignKey(Term, on_delete=models.PROTECT)
    student_class = models.ForeignKey(StudentClass, on_delete=models.PROTECT)
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT)
    ca1 = models.IntegerField(default=0)
    ca2 = models.IntegerField(default=0)
    exams = models.IntegerField(default=0)
    total = models.IntegerField(default=0)
    subject_position = models.IntegerField(default=0)


    class Meta:
        ordering = ['session']

    def __str__(self):
        return f'{self.session} {self.term} {self.subject} {self.student}'

    def grade(self):
        if (self.ca1 + self.ca2 + self.exams) >=70:
            return "A"
        elif (self.ca1 + self.ca2 + self.exams) >= 60:
            return "B"
        elif (self.ca1 + self.ca2 + self.exams) >= 50:
            return "C"
        elif (self.ca1 + self.ca2 + self.exams) >= 45:
            return "D"
        elif (self.ca1 + self.ca2 + self.exams) >= 40:
            return "E"
        else:
            return "F"

class StudentBehaviouralAssessment(SoftDeleteModel):
    student = models.ForeignKey(Student, on_delete=models.PROTECT)
    session = models.ForeignKey(Session, on_delete=models.PROTECT)
    term = models.ForeignKey(Term, on_delete=models.PROTECT)
    student_class = models.ForeignKey(StudentClass, on_delete=models.PROTECT)
    conduct = models.IntegerField(default=0)
    punctuality = models.IntegerField(default=0)
    dedication = models.IntegerField(default=0)
    participation = models.IntegerField(default=0)
    hospitality = models.IntegerField(default=0)
    neatness = models.IntegerField(default=0)
    creativity = models.IntegerField(default=0)
    physical = models.IntegerField(default=0)


    def __str__(self):
        return f'{self.session} {self.term} {self.student_class} {self.student}'

class signature(models.Model):
    classs = models.CharField(max_length=100)
    t_image = models.ImageField(upload_to='signs')
    p_image = models.ImageField(upload_to='signs')


    def __str__(self):
        return self.classs

class sets(models.Model):
    h_image = models.ImageField(upload_to='setting_images')
    g_image = models.ImageField(upload_to='setting_images')


class ReviewDecision(models.Model):
    """A correction made during data review, with enough detail to undo it.

    `before` holds the complete original rows (or field values) that the
    action changed or removed; `after` holds what was written instead.
    """
    KIND_CHOICES = (
        ('duplicate_result', 'Duplicate result'),
        ('duplicate_behaviour', 'Duplicate behavioural assessment'),
        ('result_score', 'Out-of-range result score'),
        ('behaviour_score', 'Out-of-range behavioural score'),
        ('duplicate_student', 'Duplicate student'),
        ('student_class', 'Student without class'),
        ('positions', 'Subject positions recalculated'),
        ('class_section', 'Class section restored'),
    )
    kind = models.CharField(max_length=40, choices=KIND_CHOICES)
    target = models.CharField(max_length=200, db_index=True)
    action = models.CharField(max_length=40)
    summary = models.CharField(max_length=500)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    note = models.TextField(blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(auto_now_add=True)
    undone_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.PROTECT, related_name='+')
    undone_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-decided_at', '-id')

    def __str__(self):
        return f'{self.get_kind_display()}: {self.summary}'



class UserProfile(models.Model):
    """A staff member's role and section; replaces keeping the section in first_name."""
    ADMIN = 'admin'
    HEAD = 'head'
    EXAMS = 'exams'
    STAFF = 'staff'
    ROLE_CHOICES = (
        (ADMIN, 'Administrator'),
        (HEAD, 'Head of Section'),
        (EXAMS, 'Exams Officer'),
        (STAFF, 'Staff'),
    )
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=STAFF)
    section = models.ForeignKey(Section, null=True, blank=True, on_delete=models.PROTECT)
    # The role-named accounts used before personal accounts (e.g. "exoss").
    is_legacy_shared = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)
    # The person's e-signature as a PNG data URL (drawn or uploaded).
    signature = models.TextField(blank=True, default='')

    def __str__(self):
        return f'{self.user.username} ({self.get_role_display()}, {self.section or "all sections"})'


class AuditLog(models.Model):
    """Append-only record of who changed what, when and from where."""
    ACTION_CHOICES = (
        ('create', 'Created'),
        ('update', 'Updated'),
        ('delete', 'Deleted'),
        ('restore', 'Restored'),
        ('purge', 'Permanently deleted'),
        ('login', 'Logged in'),
        ('logout', 'Logged out'),
        ('login_failed', 'Failed login'),
        ('upload', 'Upload'),
        ('bulk_delete', 'Bulk delete'),
        ('bulk_restore', 'Bulk restore'),
        ('promote', 'Promotion'),
        ('promote_undo', 'Promotion reversed'),
        ('review', 'Data review'),
        ('positions', 'Positions recalculated'),
        ('password', 'Password'),
    )
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.PROTECT, related_name='+')
    username = models.CharField(max_length=150, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True)
    model = models.CharField(max_length=60, blank=True, default='', db_index=True)
    object_id = models.CharField(max_length=40, blank=True, default='', db_index=True)
    object_repr = models.CharField(max_length=300, blank=True, default='')
    section = models.ForeignKey(Section, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    changes = models.JSONField(default=dict, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ('-timestamp', '-id')

    def __str__(self):
        return f'{self.timestamp:%Y-%m-%d %H:%M} {self.username or "system"} {self.action} {self.model} {self.object_repr}'



class ResultApproval(models.Model):
    """An e-signature on a class's results for one term.

    The class teacher / form master sign-off fills the left signature slot on
    report cards; the Head of Section approval fills the right slot and locks
    the results against changes until it is revoked. The signature image and
    every student's totals are copied at signing time, so later changes to a
    signature or to scores never alter what was approved.
    """
    TEACHER = 'teacher'
    HEAD = 'head'
    KIND_CHOICES = (
        (TEACHER, 'Class teacher / form master'),
        (HEAD, 'Head of Section'),
    )
    student_class = models.ForeignKey(StudentClass, on_delete=models.PROTECT)
    session = models.ForeignKey(Session, on_delete=models.PROTECT)
    term = models.ForeignKey(Term, on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    signer_name = models.CharField(max_length=150)
    signature = models.TextField()
    signed_at = models.DateTimeField(auto_now_add=True)
    code = models.CharField(max_length=20, unique=True)
    snapshot = models.JSONField(default=dict)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name='+')
    revoke_reason = models.CharField(max_length=300, blank=True, default='')

    class Meta:
        ordering = ('-signed_at',)

    def __str__(self):
        return f'{self.get_kind_display()} sign-off: {self.student_class} {self.session} {self.term} by {self.signer_name}'
