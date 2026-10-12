"""Student submissions, resubmissions (revisions) and reviews (feedback / grades).

Rules
-----
* **Who submits:** a student (``assignment.submit``), for themselves only: the student is
  always the caller's own student record, never an id from the request.
* **Eligibility:** the assignment is *published* and an enrollment places the student in
  its year, class and section **today in the school's time zone**. A student who moved
  to another section can no longer hand in work for the old section's assignments (their
  earlier submissions stay readable); a student who joined the section can submit its
  open assignments.
* **Deadline:** ``due_at`` is an absolute instant. A (re)submission after it is *late*
  and refused unless the assignment has ``allow_late``. Each revision snapshots the due
  time, so later deadline changes never rewrite whether earlier work was late.
* **Resubmission** appends revision N+1 (history is kept) while the assignment is
  published and ``allow_resubmission`` is set, and only until the work is reviewed:
  a review is final for the student.
* **Review:** feedback and/or marks (only when the assignment has ``max_marks``; 0 …
  max, two decimals at most) for the latest revision, by staff or faculty who manage the
  assignment, while it is published or closed. The first review needs no reason; every
  later change needs one, and each is appended to ``assignment_review_changes``.
* A student with no submission is *outstanding*, never zero marks. Assignment grades are
  separate from exam results and never touch report cards.

Concurrency: (re)submissions share-lock the assignment (closing waits for them), and
every write to a submission locks its row first. ``UNIQUE (assignment_id, student_id)``,
``UNIQUE (submission_id, revision)`` and ``UNIQUE (submission_id, sequence)`` are the
final authority.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import ColumnElement

from app.audit.service import AuditAction
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.models.assignments import (
    Assignment,
    AssignmentLifecycle,
    AssignmentReviewChange,
    AssignmentSubmission,
    AssignmentSubmissionRevision,
    SubmissionStatus,
)
from app.models.people import Student
from app.schemas.assignments import ReviewInput, SubmissionRead
from app.security.assignment_scope import (
    assignment_manageability,
    assignment_staff_reach,
    sees_submission_content,
    submission_visibility,
)
from app.security.attendance_scope import today_in_school
from app.security.people_scope import get_current_student
from app.security.school_scope import SchoolScope
from app.services.assignments import AssignmentBase, now
from app.services.students import body_error

SUB = AssignmentSubmission
NOT_ENROLLED = "You are not enrolled in this assignment's section."
SUBMISSION_CONSTRAINTS = {
    "uq_assignment_submissions_assignment_id_student_id": (
        "content",
        "You have already submitted this assignment; resubmit instead.",
    ),
    "uq_assignment_submission_revisions_submission_id_revision": (
        "content",
        "The submission changed concurrently. Please retry.",
    ),
    "uq_assignment_review_changes_submission_id_sequence": (
        "marks_awarded",
        "The review changed concurrently. Please retry.",
    ),
    "assignment_submissions_assignment_open": ("content", "The assignment is not open."),
    "assignment_revisions_late_refused": ("content", "The deadline has passed."),
    "fk_assignment_submissions_enrollment_placement": ("content", NOT_ENROLLED),
    "ck_assignment_submissions_marks_range": (
        "marks_awarded",
        "Marks must be between 0 and the maximum.",
    ),
}

type Row = tuple[AssignmentSubmission, Assignment]
type Reads = list[SubmissionRead]


class SubmissionService(AssignmentBase):
    # ------------------------------------------------------------------ helpers
    def _own_student(self, scope: SchoolScope) -> Student:
        student = get_current_student(self.session, scope.principal)
        if student is None or student.school_id != scope.school_id:
            raise PermissionDeniedError("Only students submit their own work.")
        return student

    def _visible(
        self, scope: SchoolScope, submission_id: uuid.UUID, *, for_update: bool = False
    ) -> Row:
        row = self.submissions.get_visible(
            submission_visibility(scope.principal, scope.school_id),
            submission_id,
            for_update=for_update,
        )
        if row is None:
            raise NotFoundError("Submission not found.")
        return row

    def _staff_reach(self, scope: SchoolScope, assignment_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        """Of ``assignment_ids``, those the caller reaches as staff or teaching faculty."""
        return self.assignments.visible_ids(
            assignment_staff_reach(scope.principal, scope.school_id), assignment_ids
        )

    def _open_for_work(self, scope: SchoolScope, assignment_id: uuid.UUID) -> Assignment:
        item = self.visible_assignment(scope, assignment_id, lock="share")
        if item.status != AssignmentLifecycle.PUBLISHED:
            raise ConflictError(
                f"The assignment is not open for submissions (it is {item.status})."
            )
        return item

    def _late(self, item: Assignment) -> bool:
        late = now() > item.due_at
        if late and not item.allow_late:
            raise ConflictError("The deadline has passed and late submissions are not accepted.")
        return late

    def _eligible_enrollment(
        self, scope: SchoolScope, item: Assignment, student: Student
    ) -> uuid.UUID:
        enrollment = self.submissions.eligible_enrollment(
            item, student.id, today_in_school(self.session, scope.school_id)
        )
        if enrollment is None:
            raise body_error("content", NOT_ENROLLED)
        return enrollment.id

    def _audit(
        self,
        action: AuditAction,
        scope: SchoolScope,
        submission: AssignmentSubmission,
        **extra: object,
    ) -> None:
        self.writer.audit(
            action,
            actor_id=scope.actor_id,
            resource_type="assignment_submission",
            resource_id=submission.id,
            school_id=scope.school_id,
            assignment_id=str(submission.assignment_id),
            student_id=str(submission.student_id),
            **extra,
        )

    def read(self, scope: SchoolScope, rows: Sequence[Row]) -> Reads:
        contents = self.submissions.latest_content([sub.id for sub, _ in rows])
        staff = self._staff_reach(scope, {item.id for _, item in rows})
        out: Reads = []
        for sub, item in rows:
            visible = sees_submission_content(
                scope.principal,
                self.submissions.student_user(sub.student_id),
                staff_reach=item.id in staff,
            )
            out.append(
                SubmissionRead(
                    id=sub.id,
                    school_id=sub.school_id,
                    assignment_id=sub.assignment_id,
                    student_id=sub.student_id,
                    enrollment_id=sub.enrollment_id,
                    section_id=sub.section_id,
                    status=sub.status,
                    revision_count=sub.revision_count,
                    first_submitted_at=sub.first_submitted_at,
                    last_submitted_at=sub.last_submitted_at,
                    is_late=sub.is_late,
                    content=contents.get(sub.id) if visible else None,
                    max_marks=sub.max_marks,
                    marks_awarded=sub.marks_awarded,
                    feedback=sub.feedback,
                    reviewed_revision=sub.reviewed_revision,
                    reviewed_by_user_id=sub.reviewed_by_user_id,
                    reviewed_at=sub.reviewed_at,
                )
            )
        return out

    # -------------------------------------------------------------------- reads
    def get(self, scope: SchoolScope, submission_id: uuid.UUID) -> SubmissionRead:
        return self.read(scope, [self._visible(scope, submission_id)])[0]

    def list(
        self,
        scope: SchoolScope,
        *,
        assignment_id: uuid.UUID | None,
        student_id: uuid.UUID | None,
        status: SubmissionStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[Reads, int]:
        if assignment_id is not None:
            self.visible_assignment(scope, assignment_id)  # 404 outside the caller's scope
        filters: list[ColumnElement[bool]] = [
            column == value
            for column, value in (
                (SUB.assignment_id, assignment_id),
                (SUB.student_id, student_id),
                (SUB.status, status.value if status else None),
            )
            if value is not None
        ]
        rows, total = self.submissions.list_visible(
            submission_visibility(scope.principal, scope.school_id),
            filters,
            offset=offset,
            limit=limit,
        )
        return self.read(scope, rows), total

    def revisions(
        self, scope: SchoolScope, submission_id: uuid.UUID
    ) -> Sequence[AssignmentSubmissionRevision]:
        sub, item = self._visible(scope, submission_id)
        if not sees_submission_content(
            scope.principal,
            self.submissions.student_user(sub.student_id),
            staff_reach=item.id in self._staff_reach(scope, {item.id}),
        ):
            raise PermissionDeniedError("Submitted work is visible to the student and staff only.")
        return self.submissions.revisions(sub.id)

    def review_history(
        self, scope: SchoolScope, submission_id: uuid.UUID
    ) -> Sequence[AssignmentReviewChange]:
        sub, _ = self._visible(scope, submission_id)
        return self.submissions.review_changes(sub.id)

    # ------------------------------------------------------------------- writes
    def submit(self, scope: SchoolScope, assignment_id: uuid.UUID, content: str) -> SubmissionRead:
        student = self._own_student(scope)
        item = self._open_for_work(scope, assignment_id)
        if self.submissions.find(item.id, student.id) is not None:
            raise ConflictError("You have already submitted this assignment; resubmit instead.")
        enrollment_id = self._eligible_enrollment(scope, item, student)
        late = self._late(item)
        at = now()
        submission = AssignmentSubmission(
            school_id=scope.school_id,
            assignment_id=item.id,
            student_id=student.id,
            enrollment_id=enrollment_id,
            academic_year_id=item.academic_year_id,
            class_id=item.class_id,
            section_id=item.section_id,
            max_marks=item.max_marks,
            revision_count=1,
            first_submitted_at=at,
            last_submitted_at=at,
            is_late=late,
        )

        def add() -> None:
            self.session.add(submission)
            self.session.flush()
            self.session.add(
                AssignmentSubmissionRevision(
                    school_id=scope.school_id,
                    submission_id=submission.id,
                    revision=1,
                    content=content,
                    submitted_by_user_id=scope.actor_id,
                    submitted_at=at,
                    due_at=item.due_at,
                )
            )

        self.writer.flush(SUBMISSION_CONSTRAINTS, before=add)
        # The content is the student's work: audited by length only.
        self._audit(
            AuditAction.ASSIGNMENT_SUBMISSION_CREATED,
            scope,
            submission,
            revision=1,
            is_late=late,
            content_length=len(content),
        )
        self.session.commit()
        return self.get(scope, submission.id)

    def resubmit(
        self, scope: SchoolScope, submission_id: uuid.UUID, content: str
    ) -> SubmissionRead:
        student = self._own_student(scope)
        found, _ = self._visible(scope, submission_id)
        if found.student_id != student.id:
            raise NotFoundError("Submission not found.")
        # Lock order: assignment (share), then the submission (same as review).
        item = self._open_for_work(scope, found.assignment_id)
        if not item.allow_resubmission:
            raise ConflictError("This assignment does not accept resubmissions.")
        submission, _ = self._visible(scope, submission_id, for_update=True)
        if submission.reviewed_at is not None:
            raise ConflictError("This work has been reviewed and can no longer be resubmitted.")
        self._eligible_enrollment(scope, item, student)
        late = self._late(item)
        at = now()
        revision = submission.revision_count + 1

        def apply() -> None:
            self.session.add(
                AssignmentSubmissionRevision(
                    school_id=scope.school_id,
                    submission_id=submission.id,
                    revision=revision,
                    content=content,
                    submitted_by_user_id=scope.actor_id,
                    submitted_at=at,
                    due_at=item.due_at,
                )
            )
            submission.revision_count = revision
            submission.last_submitted_at = at
            submission.is_late = late

        self.writer.flush(SUBMISSION_CONSTRAINTS, before=apply)
        self._audit(
            AuditAction.ASSIGNMENT_SUBMISSION_RESUBMITTED,
            scope,
            submission,
            revision=revision,
            is_late=late,
            content_length=len(content),
        )
        self.session.commit()
        return self.get(scope, submission.id)

    def review(
        self, scope: SchoolScope, submission_id: uuid.UUID, data: ReviewInput
    ) -> SubmissionRead:
        _, item = self._visible(scope, submission_id)
        if not self.assignments.is_visible(
            assignment_manageability(scope.principal, scope.school_id), item.id
        ):
            raise PermissionDeniedError("You may not review submissions for this assignment.")
        submission, item = self._visible(scope, submission_id, for_update=True)
        provided = data.model_fields_set
        new_marks: Decimal | None = (
            data.marks_awarded if "marks_awarded" in provided else submission.marks_awarded
        )
        new_feedback = data.feedback if "feedback" in provided else submission.feedback
        if new_marks is not None and item.max_marks is None:
            raise body_error("marks_awarded", "This assignment is not graded (no max_marks).")
        if new_marks is not None and item.max_marks is not None and new_marks > item.max_marks:
            raise body_error(
                "marks_awarded", f"Must not exceed the maximum marks ({item.max_marks})."
            )
        if new_marks is None and new_feedback is None:
            raise BusinessRuleError("A review needs marks and/or feedback.")
        first = submission.reviewed_at is None
        unchanged = (new_marks, new_feedback) == (submission.marks_awarded, submission.feedback)
        if not first and unchanged:
            return self.get(scope, submission.id)
        if not first and data.reason is None:
            raise body_error("reason", "A reason is required to change an existing review.")
        at = now()
        change = AssignmentReviewChange(
            school_id=scope.school_id,
            submission_id=submission.id,
            sequence=self.submissions.next_review_sequence(submission.id),
            revision=submission.revision_count,
            previous_marks=submission.marks_awarded,
            new_marks=new_marks,
            previous_feedback=submission.feedback,
            new_feedback=new_feedback,
            reason=data.reason,
            changed_by_user_id=scope.actor_id,
            changed_at=at,
        )
        previous_marks = submission.marks_awarded

        def apply() -> None:
            self.session.add(change)
            submission.marks_awarded = new_marks
            submission.feedback = new_feedback
            submission.reviewed_revision = submission.revision_count
            submission.reviewed_by_user_id = scope.actor_id
            submission.reviewed_at = at

        self.writer.flush(SUBMISSION_CONSTRAINTS, before=apply)
        self._audit(
            AuditAction.ASSIGNMENT_SUBMISSION_REVIEWED
            if first
            else AuditAction.ASSIGNMENT_SUBMISSION_REVIEW_CHANGED,
            scope,
            submission,
            change_id=str(change.id),
            sequence=change.sequence,
            revision=change.revision,
            previous_marks=str(previous_marks) if previous_marks is not None else None,
            new_marks=str(new_marks) if new_marks is not None else None,
            # Feedback is about the student: recorded by change, not as text.
            feedback_changed=new_feedback != change.previous_feedback,
            reason=data.reason,
        )
        self.session.commit()
        self.session.refresh(submission)
        return self.get(scope, submission.id)
