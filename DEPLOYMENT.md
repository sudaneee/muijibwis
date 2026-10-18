# Deploying the upgrade to PythonAnywhere

The live database (`db.sqlite3`) is the school's only copy of its records.
Every step below either leaves it untouched or is preceded by a verified backup.
**Never upload, copy or `git checkout` a `db.sqlite3` over the server's file.**

## Phase 0 summary

| Change | Effect on data |
|---|---|
| Django 3.1.2 → 5.2 LTS, Python 3.12 | None. `DEFAULT_AUTO_FIELD` keeps the existing id columns. |
| Migration `src.0016_protect_related_records` | SQL no-op (verify with `manage.py sqlmigrate src 0016`). Deleting a section, class, session, term, subject or student that still has records is now refused instead of deleting or orphaning them. |
| Admin bulk "delete selected" disabled | Admin records can only be deleted one at a time, with Django's confirmation page. |
| Fees and teacher-evaluation modules removed from the code | Their tables stay in the database untouched; an Excel export was taken (`archive/`, not in git). |
| Settings read from environment variables | Defaults match the current server paths. |
| `backup_db` management command | New, verified, compressed backups in `backups/`. |

Rehearsal on a copy of the live database (2026-10-18): every data table was
byte-identical before and after `migrate`; only `django_migrations` gained one
row. Report cards and broadsheets rendered by the old and new code were
byte-identical for 10 class/term combinations across SS, IE HD, IE HQ and TH HQ.

## Phase 1 summary

| Change | Effect on data |
|---|---|
| Migration `src.0017` | Adds `is_archived` to classes (Django rebuilds `src_studentclass`; ids, names and sections are preserved) and creates the `src_reviewdecision` log table. |
| Migration `src.0018` | Re-links the 19 classes left without a section when NP HQ was deleted on 2025-09-19 (ids 101–114, 137, 138, 167, 168, 177) to NP HQ and archives them. Creates the "Exams Officer" group with the 12 existing exams-officer accounts. Only rows that still match are touched. |
| Data Review screens (`/data-review`) | Exams Officers fix duplicates, out-of-range scores and duplicate students for their own section. Nothing changes until they act; every action stores the original rows and can be undone. |
| Entry validation | Result and behavioural uploads/forms refuse the whole submission if any score is outside CA1 0–20 / CA2 0–20 / Exams 0–60 / behaviour 0–5, a student is not in the selected class, or already has a result for that subject and term. |
| Archived classes | Hidden from data-entry dropdowns, still listed (grouped) in Class Result View and Subject Result Analysis. |

Rehearsal on a copy of the live database (2026-10-18), row-level diff after
`migrate`: `src_studentclass` 19 rows changed in `section_id` only (plus the new
column); `auth_group` +1, `auth_user_groups` +12, `auth_permission` +4,
`django_content_type` +1, `django_migrations` +3; every other table, including
all results, students and behavioural assessments, identical.

## Phase 2 summary

| Change | Effect on data |
|---|---|
| Migration `src.0019` | Adds `deleted_at`, `deleted_by`, `delete_reason`, `delete_batch` to students, results and behavioural assessments (Django rebuilds these three tables; every existing value and id is preserved). Creates `src_userprofile` and `src_auditlog`. |
| Migration `src.0020` | Creates a role profile for each of the 28 existing accounts (see mapping below) and removes the temporary "Exams Officer" group. |
| Roles enforced on the server | Every view checks the role and that the class belongs to the user's section; tampered requests get a 403 page. |
| Shared state removed | The multi-step entry screens no longer use global variables, so two users can enter results at the same time safely. |
| Soft delete | Deleting results/assessments needs a preview, a reason and the class name typed in; deleted rows go to the Recycle Bin and can be restored. Admin-panel deletes are soft too. |
| Audit trail | Logins (and failed logins), every create/update/delete/restore with old → new values, uploads, promotions and review decisions, with user, time and IP. Read-only. |
| Promotion | Only Heads of Section (own section) and Administrators; preview with averages first; reversible. |
| Accounts | Administrators create personal accounts on the Users page; new accounts must change their password at first login. |

Role mapping applied to existing accounts (change any of them on the Users page):

| Accounts | Role |
|---|---|
| sudaniy, mmmujaheed, admin, tempo | Administrator (all sections) |
| hosieq, hosied, hosnpd, hosnpq, pc | Head of Section |
| exo*, aexo*, examsOfficerTH, examsOfficerMA | Exams Officer |
| ahos*, vpacad, vpadmin, exth | Staff (enter and view results only) |

All role-named accounts are flagged *shared*. After deploying: create a personal
account for each staff member, then deactivate the shared accounts (and the
unused `tempo` superuser) on the Users page.

Rehearsal (2026-10-18): row-level diff after `migrate` showed no changed value
in any existing row; report cards and broadsheets rendered byte-identical to
the original Django 3.1 system; a full staff workflow (enter, correct, delete,
restore, promote, reverse) left all existing records unchanged.

## Phase 3 summary

| Change | Effect on data |
|---|---|
| Migration `src.0021` | Adds `signature` to user profiles (table rebuilt, values kept) and creates `src_resultapproval`. |
| Report cards fixed | Behaviour is matched to each student (the old pairing by list position printed another student's behaviour on 260 cards and left 617 cards unprinted across all sessions; 78 wrong and 4 missing in 2025/26). Students are alphabetical; averages and positions are unchanged (verified for all 10,946 students in 512 class/terms). |
| Broadsheet fixed | Scores line up under the right subject when a student has no result for a subject; the term shows its name; "Principal" shows for SS. |
| E-signatures | Staff add a signature on **My Signature**. On **Sign Results** the class teacher signs and the Head of Section approves; approval prints both signatures and a verification QR code on each card and locks the results until reopened. Shared accounts cannot sign. |
| Excel workflows | **Add New Students** has a template (name, gender, category) and a preview; **Update with Excel** downloads current scores, previews changes, and refuses if someone else changed a score meanwhile. |
| Forgot password | Emailed reset links (2 hours) when email is configured; otherwise users are told to ask an administrator. |
| Login protection | 5 failed logins for a username lock it for 15 minutes. |
| Error pages | Friendly 400/403/404/500 and expired-form pages; only real errors are logged. |

### Email for password reset (optional)

Add to the WSGI file, using a school mailbox (for Gmail, an app password):

```python
os.environ['DJANGO_EMAIL_HOST'] = 'smtp.gmail.com'
os.environ['DJANGO_EMAIL_PORT'] = '587'
os.environ['DJANGO_EMAIL_USER'] = 'school.mailbox@gmail.com'
os.environ['DJANGO_EMAIL_PASSWORD'] = '<app password>'
```

Staff need an email address on their account (Users page) to use it.

Full chain rehearsal (Phases 0–3 on a fresh copy, 2026-10-18): the only change
to existing values is the 19 NP HQ classes regaining their section; every
result, student and assessment value is identical.

## 1. Before deploying

1. Pick a quiet time (no result entry in progress).
2. In a PythonAnywhere Bash console, take a backup **with the old code** and
   download it to your computer:

   ```bash
   cd ~/zms/zms
   mkdir -p ~/backups
   sqlite3 db.sqlite3 ".backup '$HOME/backups/db-pre-upgrade.sqlite3'"
   sqlite3 ~/backups/db-pre-upgrade.sqlite3 "PRAGMA integrity_check; SELECT count(*) FROM src_studentresult;"
   ```

3. Fingerprint the live database (uses only the Python standard library):

   ```bash
   python3 tools/db_fingerprint.py snapshot db.sqlite3 ~/backups/before.json
   ```

   (Upload `tools/db_fingerprint.py` first if the code is not deployed yet.)

## 2. Deploy the code

1. Upload the new code **excluding** `db.sqlite3`, `media/`, `.venv/`, `backups/`, `archive/`.
2. Create a Python 3.12 virtualenv and install requirements:

   ```bash
   mkvirtualenv --python=python3.12 zms-312
   pip install -r ~/zms/zms/requirements.txt
   ```

3. On the **Web** tab: set the virtualenv to `zms-312` and the Python version to 3.12.
4. Edit the WSGI file so it sets the environment before Django loads:

   ```python
   import os
   os.environ['DJANGO_SECRET_KEY'] = '<generate a new long random string>'
   os.environ['DJANGO_DEBUG'] = '0'
   ```

   A new secret key logs everyone out once; that is expected.

5. On the **Web** tab, turn on **Force HTTPS**, and check the static file mappings exist (with `DEBUG` off,
   Django no longer serves these itself):

   | URL | Directory |
   |---|---|
   | `/static/` | `/home/muijibwis/zms/zms/static` |
   | `/media/` | `/home/muijibwis/zms/zms/media` |

## 3. Migrate and verify

```bash
cd ~/zms/zms
workon zms-312
python manage.py backup_db --label pre-deploy
python manage.py check --deploy
python manage.py migrate
python manage.py collectstatic --noinput
gunzip -c backups/db-*-pre-deploy.sqlite3.gz > ~/backups/pre-deploy.sqlite3
python3 tools/db_fingerprint.py diff ~/backups/pre-deploy.sqlite3 db.sqlite3
```

The diff must match the rehearsal above: only the tables and row counts listed
in the phase summaries change, and `src_studentresult`, `src_student` and
`src_studentbehaviouralassessment` show `same`. If anything else differs, stop
and restore (see Rollback). Then reload the web app and check: log in, open a
report card, a broadsheet, download an Excel score sheet, and open Data Review
as an exams officer.

## 4. Schedule daily backups

On the **Tasks** tab, add a daily task:

```
/home/muijibwis/.virtualenvs/zms-312/bin/python /home/muijibwis/zms/zms/manage.py backup_db
```

Keeps the newest 30 routine backups (`DJANGO_BACKUP_KEEP`); labelled ones such
as `pre-deploy` are never rotated. Download a copy off the server regularly.

## Rollback

Code: switch the Web tab back to the old virtualenv/code and reload.
Data (only if something went wrong): stop the web app, then

```bash
gunzip -c ~/zms/zms/backups/db-<timestamp>-pre-deploy.sqlite3.gz > ~/zms/zms/db.sqlite3
```

Phase 0's migration changes no SQL. Phase 1's migrations add a column and a
table, which the old code ignores, so restoring the pre-deploy backup is only
needed if the data itself must be rolled back.
