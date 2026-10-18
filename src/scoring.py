"""Score limits agreed with the school, and subject position calculation."""
from src.models import StudentResult

CA1_MAX = 20
CA2_MAX = 20
EXAM_MAX = 60
BEHAVIOUR_MAX = 5
BEHAVIOUR_FIELDS = ('conduct', 'punctuality', 'dedication', 'participation',
                    'hospitality', 'neatness', 'creativity', 'physical')


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip('-').isdigit():
        return int(value.strip())
    return None


def score_errors(ca1, ca2, exams):
    """Return a list of problems with one student's scores (empty if valid)."""
    errors = []
    for label, value, maximum in (('1st C.A', ca1, CA1_MAX), ('2nd C.A', ca2, CA2_MAX),
                                  ('Exams', exams, EXAM_MAX)):
        number = _as_int(value)
        if number is None:
            errors.append(f'{label} must be a whole number (got "{value}")')
        elif not 0 <= number <= maximum:
            errors.append(f'{label} must be between 0 and {maximum} (got {number})')
    return errors


def behaviour_errors(values):
    """`values` maps each behavioural field name to its submitted value."""
    errors = []
    for field in BEHAVIOUR_FIELDS:
        number = _as_int(values.get(field))
        if number is None or not 0 <= number <= BEHAVIOUR_MAX:
            errors.append(f'{field} must be a whole number from 0 to {BEHAVIOUR_MAX} (got "{values.get(field)}")')
    return errors


def expected_positions(results):
    """Map result id -> position: 1 + number of strictly higher totals."""
    totals = [r.total for r in results]
    return {r.id: 1 + sum(t > r.total for t in totals) for r in results}


def recompute_positions(student_class_id, subject_id, session_id, term_id):
    """Recalculate subject positions for one class/subject/session/term."""
    results = list(StudentResult.objects.filter(
        student_class_id=student_class_id, subject_id=subject_id,
        session_id=session_id, term_id=term_id))
    positions = expected_positions(results)
    changed = [r for r in results if r.subject_position != positions[r.id]]
    for r in changed:
        r.subject_position = positions[r.id]
    StudentResult.objects.bulk_update(changed, ['subject_position'])
    return len(changed)
