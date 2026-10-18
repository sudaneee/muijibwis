from django.contrib import admin
from src.models import (AuditLog, ReviewDecision, Section, Session, Student, StudentBehaviouralAssessment,
                        StudentClass, StudentResult, Subject, Term, UserProfile, sets, signature)


class SoftDeleteAdmin(admin.ModelAdmin):
    """Deleting in the admin marks the record deleted (restorable from the
    Recycle Bin) instead of removing it."""
    exclude = ('deleted_at', 'deleted_by', 'delete_reason', 'delete_batch')

    def delete_model(self, request, obj):
        obj.soft_delete(request.user, reason='Deleted in admin panel')

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            obj.soft_delete(request.user, reason='Deleted in admin panel')


class StudentAdmin(SoftDeleteAdmin):
    search_fields = ['student_name',]


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ('timestamp', 'username', 'action', 'model', 'object_repr', 'ip_address')
    list_filter = ('action', 'model')
    search_fields = ('username', 'object_repr', 'object_id')


class ReviewDecisionAdmin(ReadOnlyAdmin):
    list_display = ('decided_at', 'decided_by', 'kind', 'action', 'summary', 'undone_at')
    list_filter = ('kind', 'action')


class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'section', 'is_legacy_shared', 'must_change_password')
    list_filter = ('role', 'section', 'is_legacy_shared')


# Register your models here.
admin.site.register(Subject)
admin.site.register(StudentClass)
admin.site.register(Session)
admin.site.register(Term)
admin.site.register(Student, StudentAdmin)
admin.site.register(StudentResult, SoftDeleteAdmin)
admin.site.register(StudentBehaviouralAssessment, SoftDeleteAdmin)
admin.site.register(signature)
admin.site.register(sets)
admin.site.register(Section)
admin.site.register(UserProfile, UserProfileAdmin)
admin.site.register(AuditLog, AuditLogAdmin)
admin.site.register(ReviewDecision, ReviewDecisionAdmin)
