from django.urls import path
from django.contrib.auth import views as auth_views

from . import accounts, bulk, promotion, review, signing, trail, views


urlpatterns = [
    path('', views.home, name='home'),
    path('create-result', views.resultcreate, name='resultcreate'),
    path('update-result', views.updateresult, name='updateresult'),
    path('single-result-update', views.single_result_update, name='single_result_update'),
    path('bulk-result-view', views.result_view, name='result_view'),
    path('class-result-summary', views.class_result_summary, name='class_result_summary'),
    path('settings', views.settings, name='settings'),
    path('single-result-view', views.single_result_view, name='single_result_view'),
    path('bulk-result-upload', views.ResultUpload, name='std_upload'),
    path('create-student', bulk.student_upload, name='std_create'),
    path('set-position', views.setting_position, name='setting_position'),
    path('nav', views.side_nav, name='nav'),
    path('score-sheet', views.score_sheet, name='score_sheet'),
    path('logout', views.logout, name='logout'),
    path('excel_score_sheet', views.excel_score_sheet, name='excel_score_sheet'),
    path('not-uploaded-results', views.not_uploaded_results, name='not_uploaded_results'),
    path('behaivioural-excel', views.bhvxl, name='bhvxl'),
    path('behaivioural-excel-missed', views.bhvxl_missed, name='bhvxl-missed'),
    path('behaivioural-excel2', views.behav_sheet, name='behav_sheet'),
    path('std-alts', views.std_alts, name='Std-Alts'),
    path('result-summary', views.class_summary, name='class-result-summary'),
    path('delete_result', views.delete_result, name='delete_result'),
    path('delete_bhv', views.delete_bhv, name='delete_bhv'),
    path('promote', promotion.promote, name='promote'),
    path('update-result-excel', bulk.result_excel_update, name='result_excel_update'),
    path('sign-results', signing.sign_results, name='sign_results'),
    path('my-signature', signing.my_signature, name='my_signature'),
    path('verify/<str:code>/<int:student_id>/<str:token>', signing.verify_result, name='verify_result'),
    path('password-reset', accounts.PasswordResetRequest.as_view(), name='password_reset'),
    path('password-reset/sent', auth_views.PasswordResetDoneView.as_view(
        template_name='src/password_reset_done.html'), name='password_reset_done'),
    path('password-reset/<uidb64>/<token>', accounts.PasswordResetConfirm.as_view(), name='password_reset_confirm'),
    path('password-reset/complete', auth_views.PasswordResetCompleteView.as_view(
        template_name='src/password_reset_complete.html'), name='password_reset_complete'),
    path('recycle-bin', trail.recycle_bin, name='recycle_bin'),
    path('audit-trail', trail.audit_log, name='audit_log'),
    path('users', accounts.users, name='users'),
    path('users/<int:user_id>', accounts.user_edit, name='user_edit'),
    path('password-change', accounts.password_change, name='password_change'),
    path('data-review', review.review_home, name='review_home'),
    path('data-review/duplicates', review.review_duplicates, name='review_duplicates'),
    path('data-review/scores', review.review_scores, name='review_scores'),
    path('data-review/behaviour', review.review_behaviour, name='review_behaviour'),
    path('data-review/students', review.review_students, name='review_students'),
    path('data-review/positions', review.review_positions, name='review_positions'),
    path('data-review/export', review.review_export, name='review_export'),
    path('data-review/undo/<int:decision_id>', review.review_undo, name='review_undo'),


]

