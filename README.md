# Smart School Platform

Centralized school management platform: parent, student, faculty and management portals.

**Status:** Phase 8 complete — assignments and submissions: section-and-subject
coursework with a draft → published → closed lifecycle, faculty-scoped management,
student text submissions with an append-only revision history, explicit deadlines and
late rules, and reviews (feedback, optional grades) with an append-only change history.
It builds on Phase 7 (examinations), Phase 6 (attendance), Phase 5 (faculty and
assignments), Phase 4 (students, parents, enrollments), Phase 3 (schools, academic
structure), Phase 2 (authentication, RBAC, audit) and Phase 1 (FastAPI, PostgreSQL,
Alembic, Docker, tests).

> **Phase 9 (timetable) has NOT started.** Not implemented yet: timetable, announcements,
> notifications, fees, reports and dashboards (beyond attendance summaries and exam report
> cards), file attachments, PDF report cards, AI and the frontend portals.

## Architecture

A **modular monolith** (no microservices). The backend is layered:

```
API routes (app/api/v1)  →  schemas (app/schemas, Pydantic v2)
  →  services (app/services, business rules)  →  repositories (app/repositories)
  →  SQLAlchemy models (app/models)  →  PostgreSQL
```

```
backend/
  app/
    main.py            application factory (create_app) + ASGI entrypoint
    config.py          typed settings from environment variables
    database.py        engine / session management
    cli.py             admin CLI: create-school, create-user
    api/deps.py        shared dependencies: settings, DB session, pagination,
                       authentication (get_current_principal) and authorization
                       (require_permission / require_role)
    api/v1/            versioned routers: health, auth, roles, users, school,
                       academic_years, classes, sections, subjects, students,
                       parents, enrollments, faculty, faculty_assignments,
                       attendance, exams, assignments
    core/              cross-cutting infrastructure: logging, errors, middleware,
                       request context (IP / user agent / request id)
    models/            ORM models: users, RBAC, auth sessions/tokens/OTP, audit log,
                       schools, academic years, classes, sections, subjects,
                       students, parents, student_parents, student_enrollments,
                       faculty, faculty_assignments, attendance, exams
                       (exams, exam_subjects, exam_marks, exam_mark_changes,
                       exam_publications, exam_report_cards), assignments
                       (assignments, assignment_submissions, revisions,
                       review changes)
    schemas/           request/response models, shared envelopes
    services/          business logic: auth, OTP sign-in, RBAC, user provisioning,
                       school profile, academic structure, students, parents,
                       enrollments, faculty and faculty assignments,
                       attendance, exams, marks and results (the arithmetic
                       lives in result_calculation), assignments and
                       submissions
    repositories/      data access
    security/          passwords, JWT/refresh tokens, OTP, RBAC catalog,
                       principal, resource-scope framework, school scope,
                       people scope, faculty scope, attendance scope, exam scope,
                       assignment scope
    audit/             audit-trail writer with secret redaction
    utils/             small generic helpers
  alembic/             migrations
  tools/               developer verification tools (E2E, mutation testing)
  tests/unit           no database needed
  tests/integration    real PostgreSQL test database
```

Deviations from the initial outline, and why:

- **`app/core/`** was added for logging, error handling and middleware. They are
  application infrastructure, not generic helpers, so `utils/` was the wrong place.
- **Synchronous SQLAlchemy with psycopg 3.** FastAPI runs sync endpoints in a threadpool. This
  keeps transactions, tests and debugging simple; async can be adopted later if profiling
  shows it is needed.
- **No global engine.** `create_app(settings)` builds the `Database` and stores it on
  `app.state`. That lets tests run against an isolated `_test` database.
- Domain router files (`students.py`, `attendance.py`, ...) are added in their own phases rather
  than as empty stubs.

### Conventions

- Base path `/api/v1`. A single resource is returned as the object itself. Lists return
  `{"items": [...], "total", "page", "page_size", "pages"}` (`?page=1&page_size=20`, max 100).
- Every error returns `{"error": {"code", "message", "details", "request_id"}}`. Validation
  errors never echo submitted values, and 500s never expose internals.
- Every response carries `X-Request-ID`. A valid incoming id is propagated; otherwise one is
  generated. The id appears on every log line.
- Models use UUID primary keys, timezone-aware `created_at`/`updated_at`, and deterministic
  constraint names. Services raise `AppError` subclasses (`NotFoundError`,
  `PermissionDeniedError`, ...); they do not raise HTTP exceptions.

## Getting started

Prerequisites: Docker Desktop and Python 3.12+.

```bash
cp .env.example .env            # then set POSTGRES_PASSWORD to a strong random value
docker compose up -d --build    # PostgreSQL + backend (runs `alembic upgrade head` on start)
curl http://127.0.0.1:8000/api/v1/health
```

API docs (when `API_DOCS_ENABLED=true`): http://127.0.0.1:8000/api/v1/docs

If port 5432 is already used on your machine, set `POSTGRES_PORT` in `.env` (for example
`5434`). Use `127.0.0.1`, not `localhost`, for `POSTGRES_HOST`.

### Local backend development (outside Docker)

```bash
docker compose up -d db
cd backend
python -m venv .venv
.venv/Scripts/activate           # Windows (Git Bash: source .venv/Scripts/activate)
# source .venv/bin/activate      # macOS / Linux
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

### Test status

1044 tests pass (153 of them added in Phase 7) with 99% coverage. Ruff format and lint
are clean and mypy strict reports no issues. Integration tests run against real
PostgreSQL, including:
- concurrency tests (`test_concurrency.py`, `test_people_concurrency.py`,
  `test_faculty_concurrency.py`, `test_attendance_concurrency.py`,
  `test_exam_concurrency.py`);
- IDOR tests for parents and students (`test_people_authorization.py`), for faculty
  (`test_faculty_authorization.py`), for attendance (`test_attendance_authorization.py`) and
  for exams (`test_exam_authorization.py`);
- raw-SQL constraint tests (`test_*_constraints.py`);
- migration preservation tests (`test_migration_0003.py` to `test_migration_0007.py`).

### Quality checks (run from `backend/`)

```bash
ruff format .            # format
ruff check .             # lint (includes bandit security rules)
mypy                     # strict type checking
pytest --cov             # tests; integration tests need `docker compose up -d db`
```

Integration tests create and migrate a separate database (`TEST_POSTGRES_DB`, default
`smart_school_test`). The suite refuses to run against a database whose name does not end in
`_test`. Each test runs inside a transaction that is rolled back.

### Migrations

```bash
alembic upgrade head                                   # apply (also runs on backend container start)
alembic current                                        # show the applied revision
alembic downgrade -1                                   # undo the latest migration
alembic revision --autogenerate -m "describe change"   # review the generated file!
# inside Docker: docker compose exec backend alembic upgrade head
```

| Revision | Contents |
|---|---|
| `0001_baseline` | Empty root of the history |
| `0002_auth_rbac` | users, roles, permissions, user_roles, role_permissions, auth_sessions, refresh_tokens, otp_challenges, login_attempts, audit_logs, plus the seeded roles, permissions and matrix |
| `0003_school_academic` | schools, academic_years, classes, sections, subjects; the `btree_gist` extension; `fk_users_school_id_schools` |
| `0004_students_parents` | students, parents, student_parents, student_enrollments; `UNIQUE (id, school_id)` on academic_years, sections, subjects and users, plus `UNIQUE (id, class_id)` on sections, as composite-FK targets. Additive only: no existing rows are touched. |
| `0005_faculty_assignments` | faculty, faculty_assignments, with composite same-school foreign keys and the partial unique index for active assignments. No existing table is modified and no rows are created. |
| `0006_attendance` | attendance_sessions, attendance_records, attendance_record_changes (+ append-only trigger); `UNIQUE (id, school_id, student_id, academic_year_id, class_id, section_id)` on student_enrollments as a composite-FK target. Additive only: no existing column or row changes, no rows are created. |
| `0008_assignments_submissions` | assignments, assignment_submissions, assignment_submission_revisions, assignment_review_changes, their composite same-school keys and lifecycle/append-only triggers; the `assignment.submit` permission granted to STUDENT. No other existing row or column changes. |
| `0007_examinations_marks` | exams, exam_subjects, exam_marks, exam_mark_changes, exam_publications, exam_report_cards, their composite same-school keys and the lifecycle/append-only triggers. Uses only existing composite-FK targets: no existing table, column or row changes, no rows are created. |

`tests/integration/test_database.py` fails if models and migrations drift apart, and runs
a full downgrade → upgrade round trip.

`users.school_id` now references `schools.id` (migration 0003, `ON DELETE RESTRICT`). It
stays nullable because NULL is the correct value for platform-level SUPER_ADMIN accounts.
The migration preserves existing users. If a user already carries a `school_id` that
doesn't exist, the upgrade **stops with an explanation**: it never invents schools or
discards data. Migration 0003 needs the `btree_gist` extension, which is trusted on
PostgreSQL ≥ 13. Downgrading leaves the extension installed.

## Authentication

### Flows

| Who | How | Endpoint | Status |
|---|---|---|---|
| Admin / management | email + password | `POST /auth/login` | Implemented |
| Faculty | email **or** mobile + password | `POST /auth/login` | Implemented |
| Parent | mobile + one-time code | `POST /auth/otp/request` → `POST /auth/otp/verify` | Implemented (SMS is a dev stub) |
| Student | school code + admission number + password | `POST /auth/login` with `school_code` | Implemented (Phase 4) |

`/auth/login` decides from the identifier itself whether it is an email address (contains
`@`) or a mobile number (international format, for example `+919876543210`). With
`school_code`, the identifier is a student's admission number in that school (see
[Student identity](#student-and-parent-identity)). Other identifiers are treated exactly
like an unknown account.

All successful sign-ins return the same token pair:

```json
{"access_token": "...", "token_type": "bearer", "expires_in": 900,
 "refresh_token": "...", "refresh_expires_in": 604800}
```

### Endpoints (`/api/v1`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/login` | — | Password sign-in → token pair |
| POST | `/auth/refresh` | refresh token | Rotate refresh token, new access token |
| POST | `/auth/logout` | refresh token | Revoke the session (idempotent, 204) |
| GET | `/auth/me` | `auth.me` | Profile, roles, effective permissions |
| POST | `/auth/otp/request` | — | Send a sign-in code (always 202, same body) |
| POST | `/auth/otp/verify` | — | Code → token pair |
| GET | `/roles` | `role.read` | Roles with their permissions (paginated) |
| GET | `/permissions` | `permission.read` | Permission catalog (paginated) |
| GET | `/users/{id}/roles` | `user.read` | A user's roles (same school only) |
| PUT | `/users/{id}/roles` | `user.assign_role` | Replace a user's roles (audited) |

### Tokens and sessions

- **Access token:** an HS256 JWT valid for 15 minutes. Its claims are `sub` (user id),
  `sid` (session id), `typ=access`, `jti`, `iat`, `nbf`, `exp`, `iss` and `aud`. It contains
  no personal data and no roles. The algorithm is pinned, so `alg=none` and algorithm
  confusion are rejected.
- **Every request** re-checks in the database that the session is live and the user is
  active, and loads roles and permissions fresh. Logout, deactivation, role changes and
  reuse detection therefore take effect immediately, not when the token expires.
- **Refresh token:** an opaque random value (48 bytes). Only its SHA-256 digest is stored.
  Each login creates one `auth_session`, which is one refresh-token family. Every refresh:
  1. locks the token row;
  2. checks it is not expired, not revoked, the session is live and the user is still
     eligible;
  3. revokes the old token and links it to the new one (`replaced_by_id`);
  4. issues a new pair.
- **Reuse detection:** if a revoked or already-rotated refresh token is presented, the whole
  session is revoked, along with every token in the family and its access tokens. The
  event is audited as `auth.refresh.reuse_detected`, and the user must sign in again.
- **Session lifetime:** refresh tokens last 7 days each, and a session can last at most
  30 days in total, however often it is refreshed.

### OTP (parent sign-in)

- Codes are 6 digits by default, generated with `secrets`, and expire after 5 minutes.
- Codes are stored only as HMAC-SHA256 keyed with `SECRET_KEY` and bound to the challenge
  id. A database dump therefore cannot be brute-forced, and a code cannot be replayed
  against another challenge.
- A code allows at most 5 verification attempts. After that it is invalidated, even if
  the correct code is entered later.
- A code is single use, and requesting a new code invalidates the previous one. A partial
  unique index enforces at most one live code per number.
- Rate limits: a 60-second resend cooldown, 5 requests per number per hour and 30 per IP per
  hour. Exceeding them returns `429` with `Retry-After`.
- **No account enumeration:** unknown or inactive numbers get the identical `202` response
  and the same rate limits. Nothing is sent, and verification can never succeed. The
  mobile number is stored only as an HMAC fingerprint.

### Local development behaviour

- `OTP_DELIVERY_BACKEND=development_console` is a **development stub**: codes are written to
  the backend log (`docker compose logs backend`), with the phone number masked.
  Configuration validation rejects it in staging and production. The default, `disabled`,
  makes OTP endpoints return `503`. No real SMS provider is integrated yet.
- If `SECRET_KEY` or `JWT_SECRET_KEY` is unset or a placeholder in development, a random
  per-process key is used, so sessions end when the backend restarts. Staging and production
  refuse to start without real keys of at least 32 characters.
- Tests use a test-only in-memory OTP channel and cheap Argon2 parameters. Production code
  paths are unchanged.

**Create the first administrator** (the password is prompted for; it is never accepted on
the command line):

```bash
docker compose exec backend python -m app.cli create-user \
    --full-name "Platform Admin" --email admin@your-school.example --role SUPER_ADMIN
# school-level accounts need --school-id <uuid>; parents: --phone +91... --role PARENT --no-password
# non-interactive: --password-env VAR_NAME (reads the password from that environment variable)
```

## Authorization

Authorization works in two layers, both enforced server-side. Hiding UI controls is not
security.

1. **RBAC:** `require_permission(PermissionName.X)` / `require_role(RoleName.Y)` FastAPI
   dependencies. A missing token returns `401`; a missing permission returns `403`.
   Permissions are read from the database per request.
   ```python
   @router.get("/students")
   def list_students(
       principal: Annotated[Principal, Depends(require_permission(PermissionName.STUDENT_READ))],
   ): ...
   ```
2. **Resource scope** (`app/security/authorization.py`) limits *which* records a permission
   reaches:
   - `ensure_school_access` enforces the tenant boundary: non-super-admins only see records
     in their own school, and other schools' records return `404`, not `403`, so their
     existence is not revealed.
   - `resolve_student_scope` maps a principal to `ALL_SCHOOLS`, `SCHOOL`, `ASSIGNED`,
     `LINKED_CHILDREN`, `SELF` or `NONE`.
   - `ensure_student_access` is the single check every student-data endpoint will call.

> **Relationship scopes are database-backed:** parent → linked children and student → self
> (Phase 4) and faculty → assigned students (Phase 5) are implemented by
> `DbStudentAccessPolicy`. `DenyAllStudentAccessPolicy` remains the fail-closed default.
> Persona permissions such as `attendance.read` held by PARENT/STUDENT/FACULTY are only
> safe to expose through endpoints that apply these scope checks.

**Role assignment rules:**
- An actor may only grant or remove roles listed in `ASSIGNABLE_ROLES` for their own
  roles. Only SUPER_ADMIN can grant or remove SUPER_ADMIN.
- Nobody can change their own roles.
- Targets must be in the actor's school.

### Role–permission matrix

Source of truth: `backend/app/security/permissions.py`, seeded by migration
`0002_auth_rbac`. A test fails if the database and the code disagree.

| Permission | SUPER | SCH_ADMIN | MGMT | FACULTY | PARENT | STUDENT |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| `auth.login` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `auth.refresh` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `auth.logout` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `auth.me` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `user.read` | ✓ | ✓ | ✓ |  |  |  |
| `user.create` | ✓ | ✓ |  |  |  |  |
| `user.update` | ✓ | ✓ |  |  |  |  |
| `user.deactivate` | ✓ | ✓ |  |  |  |  |
| `user.assign_role` | ✓ | ✓ |  |  |  |  |
| `role.read` | ✓ | ✓ | ✓ |  |  |  |
| `role.manage` | ✓ |  |  |  |  |  |
| `permission.read` | ✓ | ✓ |  |  |  |  |
| `permission.manage` | ✓ |  |  |  |  |  |
| `school.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `school.manage` | ✓ | ✓ |  |  |  |  |
| `student.read_self` |  |  |  |  |  | ✓ |
| `student.read` | ✓ | ✓ | ✓ |  |  |  |
| `student.create` | ✓ | ✓ |  |  |  |  |
| `student.update` | ✓ | ✓ |  |  |  |  |
| `student.deactivate` | ✓ | ✓ |  |  |  |  |
| `parent.read_self` |  |  |  |  | ✓ |  |
| `parent.read_child` |  |  |  |  | ✓ |  |
| `faculty.read_self` |  |  |  | ✓ |  |  |
| `faculty.read_assigned_students` |  |  |  | ✓ |  |  |
| `faculty.manage` | ✓ | ✓ |  |  |  |  |
| `attendance.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `attendance.manage` | ✓ | ✓ |  | ✓ |  |  |
| `academic.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `academic.manage` | ✓ | ✓ |  |  |  |  |
| `marks.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `marks.manage` | ✓ | ✓ |  | ✓ |  |  |
| `marks.publish` | ✓ | ✓ | ✓ |  |  |  |
| `assignment.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `assignment.manage` | ✓ | ✓ |  | ✓ |  |  |
| `assignment.submit` |  |  |  |  |  | ✓ |
| `timetable.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `timetable.manage` | ✓ | ✓ |  |  |  |  |
| `announcement.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `announcement.manage` | ✓ | ✓ | ✓ |  |  |  |
| `notification.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `notification.manage` | ✓ | ✓ |  |  |  |  |
| `report.read` | ✓ | ✓ | ✓ |  |  |  |
| `analytics.read` | ✓ | ✓ | ✓ |  |  |  |
| `ai.read` | ✓ | ✓ | ✓ |  |  |  |
| `ai.manage` | ✓ | ✓ |  |  |  |  |

Grant counts: SUPER_ADMIN 39, SCHOOL_ADMIN 37, MANAGEMENT 20, FACULTY 17, PARENT 14, STUDENT 14
(`assignment.submit` was added in Phase 8; see migration 0008).

Design notes:
- PARENT and STUDENT can only read, scoped to their children or themselves. FACULTY manage
  attendance, marks and assignments for assigned classes, but cannot publish results,
  create students or users, assign roles or manage the school.
- MANAGEMENT has oversight: it can read school-wide, publish results and manage
  announcements, but does not enter marks or attendance.
- SCHOOL_ADMIN has everything except changing the role and permission catalog.
  SUPER_ADMIN is platform-wide.
- Persona-only permissions (`*.read_self`, `parent.read_child`,
  `faculty.read_assigned_students`) are deliberately not given to administrators.
- Adding a permission later requires both a code change and a new migration. There is no
  implicit super-admin bypass.

## Audit log

The `audit_logs` table records:
- sign-in success, failure (with a reason) and throttling;
- logout;
- refresh success and failure, and refresh-token reuse;
- OTP requested, failed, verified and throttled;
- role changes (with before and after);
- user creation.

Each row stores the actor, outcome, resource, IP address (validated), user agent
(sanitised and truncated), request id and JSONB metadata. Before persisting, any metadata
key resembling a secret (`password`, `token`, `otp`, `code`, `secret`, `hash`,
`authorization`, ...) is redacted. Passwords, OTPs and tokens are never written; tests scan
every auth table after exercising all flows to prove it.

## School and academic structure

```
School  (tenant boundary; never hard-deleted)
  ├── Academic Year   (no overlaps per school; at most one current)
  ├── Class           (grade level, e.g. "Class 10")
  │     └── Section   (e.g. "A"; always in the same school as its class)
  └── Subject
```

Users belong to one school through `users.school_id` (SUPER_ADMIN has none). Student
enrollments, faculty assignments and timetables will link these entities in later phases.

### Entities and rules

| Entity | Key fields | Uniqueness | Other rules |
|---|---|---|---|
| `schools` | name, code, address, city, state, postal_code, country, phone, email, timezone (default `UTC`), logo_url, is_active | `code` unique platform-wide | `code` is immutable over the API. Inactive schools suspend their users. |
| `academic_years` | school_id, name, start_date, end_date, is_current, is_active | name unique per school | `start_date < end_date`; no overlapping years per school (inclusive dates); at most one current year, which must be active |
| `classes` | school_id, name, code, display_order, is_active | code unique per school | Can't be deactivated while it has active sections |
| `sections` | school_id, class_id, name, code, capacity, is_active | code unique per class | capacity > 0 if set; class must be in the same school; `class_id` is immutable; can't be created under, or reactivated under, an inactive class |
| `subjects` | school_id, name, code, description, is_active | code unique per school | — |

Nothing in this phase is hard-deleted: there are no DELETE endpoints, records are
deactivated with `PATCH {"is_active": false}`, and every foreign key is `ON DELETE RESTRICT`.

**Input normalisation** (documented, nothing else is changed):
- Names and text are trimmed; blank values are rejected.
- Codes are trimmed and upper-cased, then must match `[A-Z0-9][A-Z0-9_-]*` (max 30
  characters), so `"c10"` and `"C10"` collide.
- Emails are lower-cased, and phones are converted to E.164.
- Timezones must be IANA names, and logo URLs must be `https://`.
- `PATCH` rejects an empty body, unknown or immutable fields, and `null` for required fields.

### Endpoints (`/api/v1`)

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/school` | `school.read` | The caller's school |
| PATCH | `/school` | `school.manage` | `code` is immutable; `is_active` is SUPER_ADMIN only; audited |
| GET | `/schools` | SUPER_ADMIN | All schools, paginated, so a SUPER_ADMIN can pick an `X-School-ID` |
| POST, GET | `/academic-years` | `academic.manage` / `academic.read` | List filters: `is_active`, `is_current` |
| GET, PATCH | `/academic-years/{id}` | read / manage | |
| POST | `/academic-years/{id}/set-current` | `academic.manage` | Atomic switch; idempotent |
| POST, GET | `/classes` | manage / read | Filter: `is_active`; ordered by `display_order` |
| GET, PATCH | `/classes/{id}` | read / manage | |
| POST, GET | `/sections` | manage / read | Filters: `class_id`, `is_active` |
| GET, PATCH | `/sections/{id}` | read / manage | |
| POST, GET | `/subjects` | manage / read | Filter: `is_active` |
| GET, PATCH | `/subjects/{id}` | read / manage | |

Creates return `201`. Lists are paginated (`page`, `page_size` ≤ 100). Duplicates and
overlaps return `409`; invalid input and broken business rules return `422`.

Schools are created by a platform operator with the CLI, not over HTTP:
```bash
docker compose exec backend python -m app.cli create-school --name "Green Hills School" \
    --code GHS --timezone Asia/Kolkata
```

### Authorization and same-school isolation

Phase 3 reuses the Phase 2 permissions; **no permissions or role grants were changed**.

| Role | School profile | Academic structure |
|---|---|---|
| SUPER_ADMIN | read/update any school (selected with `X-School-ID`); can (de)activate | full, in the selected school |
| SCHOOL_ADMIN | read/update own school (not `is_active`) | full, own school |
| MANAGEMENT | read own school | read own school |
| FACULTY | read own school | read own school |
| PARENT / STUDENT | read own school | read own school* |

\* PARENT and STUDENT hold `school.read` and `academic.read` from the Phase 2 matrix. In
Phase 3 that lets them read their own school's non-personal structure (years, classes,
sections, subjects). Child- and self-scoped data access arrives with Phase 4. No
parent–child logic exists yet.

How the target school is resolved (`app/security/school_scope.py`, applied to every
school-scoped endpoint through `require_school_permission`):
- **School users** always act on their own `users.school_id`. Sending `X-School-ID` for any
  other school returns **404**, exactly as for a non-existent school, so other schools'
  existence is never revealed. That is the same policy as Phase 2's `ensure_school_access`.
- **SUPER_ADMIN** must name the school in the `X-School-ID` header (otherwise `400
  school_selection_required`). A header rather than a query parameter keeps the selection
  out of URLs and logs and works the same on every endpoint.
- Every query and update filters by `school_id`, so another school's record IDs return
  **404** on GET and PATCH. A `class_id` from another school in a section body returns the
  same `422 "Class not found."` as an unknown id.
- The database enforces isolation too. Sections reference their class with a composite
  foreign key, `(class_id, school_id) → classes(id, school_id)`, so a cross-school section
  can't be stored even by buggy code.
- **Inactive schools:** users of an inactive school can't sign in or refresh, and their
  existing access tokens stop working immediately. This was a small, deliberate change to
  the Phase 2 eligibility checks; failed logins are audited with reason `school_inactive`.
  The school-scope check also returns `403 school_inactive` as a second safeguard. Only
  SUPER_ADMIN can deactivate or reactivate a school; its history is preserved.

### Academic-year rules and concurrency strategy

Correctness is enforced by PostgreSQL, so it holds under concurrent requests rather than
relying on a Python pre-check that could race:

- **No overlaps:** the exclusion constraint `ex_academic_years_no_overlap` (GiST over
  `school_id` and `daterange(start_date, end_date, '[]')`). Two concurrent overlapping
  creates can't both commit; the loser gets `409`.
- **One current year:** the partial unique index `uq_academic_years_one_current_per_school`
  (`WHERE is_current`), and a current year must be active (`CHECK`).
- **`set-current`** runs in one transaction:
  1. lock the school row (`SELECT … FOR UPDATE`);
  2. lock the target year;
  3. clear the existing current year;
  4. set the new one, then commit.

  Concurrent switches for one school queue up behind each other, and the last one wins;
  there is never a moment with two current years. The unique index would still catch a code
  path that skipped the lock.
- **Unique codes and names** (schools, classes, sections, subjects, year names) are unique
  constraints. Services flush inside a SAVEPOINT and map violated constraint names to clean
  `409` and `422` responses; raw database messages are never returned.
- **Class deactivation versus new sections:** both lock the class row, so a class can never
  end up inactive with an active section.

`tests/integration/test_concurrency.py` verifies this with committed data and real
threads, using separate connections. Deterministic tests hold a conflicting transaction
open and assert that the second writer *blocks* and then fails cleanly. Stress tests release
several threads together and check the invariant every time. As a mutation check, removing
the school lock or the class lock makes these tests fail.

### Audit events

`school.created` (CLI), `school.updated`, `academic_year.created`, `academic_year.updated`,
`academic_year.set_current` (with the previous current year), and for classes, sections and
subjects: `.created`, `.updated`, `.deactivated` and `.reactivated`.

Update events record `{"field", "from", "to"}` entries plus `school_id`, so field names are
values rather than JSON keys and survive the secret redaction from Phase 2.

## Students, parents and enrollments

```
School
 ├── Academic Years
 │     └── Enrollments ──┬── Student
 │                       ├── Class
 │                       └── Section (must belong to that Class)
 ├── Students
 ├── Parents
 │     └── Student relationships (student_parents)
 ├── Classes
 ├── Sections
 └── Subjects
```

### Student and parent identity

`users` remains the **only** authentication identity: credentials, roles, sessions and
tokens. Domain records link to it, so Phase 2's login, refresh, logout, throttling and
audit apply unchanged.

- **Parent:** `parents.user_id` is required and unique, pointing at a PARENT user. The
  mobile number and email live on `users` only, because `users.phone` is what OTP sign-in
  looks up. The API returns them as the parent's `mobile` and `email`. Parents have no
  password.
- **Student:** `students.user_id` is optional and unique. It is created when a password is
  supplied at `POST /students`, and that STUDENT user has no email or phone.
  - **Deliberate deviation:** there is no `password_hash` column on `students`. The hash
    lives on `users.password_hash`, so there is exactly one credential store.
- **Canonical student identifier:** the **admission number**. It is unique per school,
  trimmed, upper-cased, and must match `[A-Z0-9][A-Z0-9/_-]*` (up to 30 characters).
  - It is the login ID, so it is **immutable** over the API.
  - There is deliberately no second "student code".
- **Student sign-in:** `POST /auth/login {"school_code", "identifier": admission number,
  "password"}`. The school code is needed because admission numbers are only unique within
  a school. Sign-in resolves `schools.code → students.admission_number → students.user_id`
  to the user, so it always lands on a real student record.
  - Failures return the same generic 401 and are throttled exactly like other logins.
  - JWTs carry no student data.
- **Status sync:** a student whose status isn't `active`, or a deactivated parent, has
  their user set to `deactivated`. Phase 2 then rejects sign-in, refresh and existing
  access tokens immediately. Reactivation restores access.

### Authorization

All reads go through one SQL visibility condition per resource
(`app/security/people_scope.py`). Lists and single-record reads use the same condition, so
replacing an ID in a URL or a filter can't escape it. Anything outside the caller's scope
returns **404**, indistinguishable from an unknown ID.

| Caller | Students | Enrollments | Parents | Links |
|---|---|---|---|---|
| SUPER_ADMIN (selected school), SCHOOL_ADMIN, MANAGEMENT (`student.read`) | the whole school | the whole school | the whole school (`user.read`) | all, including deactivated history |
| PARENT (`parent.read_child`, `parent.read_self`) | children with an **active** link to their **active** parent record | their children's | **only themselves** | only their own links, never co-parents |
| STUDENT (`student.read_self`) | only themselves | only their own | none (403) | their own active parent links |
| FACULTY (`faculty.read_assigned_students`, Phase 5) | students with an active enrollment in an assigned year/class/section, contact details withheld | only enrollment rows matching an assignment | none (403) | none (empty list) |

- **Filters only narrow.** `?student_id=…`, `?class_id=…` and the other filters are ANDed
  with the caller's visibility.
- **Writes are staff-only** and use existing permissions; nothing new was added to the
  catalog:
  - student create and update use `student.create` and `student.update`;
  - status changes also need `student.deactivate`;
  - parent create and update use `user.create` and `user.update`, because a parent is a
    user account, with `user.deactivate` needed to (de)activate one;
  - links and enrollments use `student.update`.

  PARENT and STUDENT have none of these permissions, so they can't modify anything.
- **Reusable helpers:**
  - `student_visibility`, `parent_visibility`, `enrollment_visibility` and
    `link_visibility`;
  - `get_current_parent` and `get_current_student`;
  - `DbStudentAccessPolicy`, which implements the Phase 2 `StudentAccessPolicy` hook with
    `ensure_student_access` for later phases (its faculty method is live since Phase 5).

### Enrollment history and current enrollment

- **Append-only.** An enrollment's student, year, class and section never change. Updates
  can only touch the number, dates, status (`active`, `completed`, `withdrawn`) and
  `is_current`.
- **One active enrollment per student per academic year,** enforced by a partial unique
  index. A second one in the same year returns 409.
- **Mid-year moves** to another class or section: `POST /enrollments` with
  `supersedes_enrollment_id`.
  - The old row becomes `superseded`, with `end_date` set to the move date. It stays in the
    history and becomes read-only.
  - If the old row was current, the new one becomes current.
  - Nothing is overwritten.
- **At most one current enrollment per student,** enforced by a partial unique index, and
  a current enrollment must be `active` (CHECK).
  - Setting `is_current` on create or PATCH clears the previous current enrollment in the
    same transaction.
  - Leaving `active` status always ends currency.
  - Back-filled history (`is_current: false`, the default) never takes currency.
- **Validation:**
  - the year, class and section must exist in the school and be active;
  - the section must belong to the class;
  - the student must be active;
  - `enrollment_date` must fall within the academic year;
  - `end_date` must be on or after `enrollment_date`.
- **Concurrency:** every enrollment write locks the student row (`SELECT … FOR UPDATE`)
  before touching enrollments, so concurrent changes for one student queue up. The unique
  indexes catch anything that skips the lock.

### Same-school enforcement in the database

Every cross-table reference carries `school_id` and targets a composite key:

| From | Composite foreign key → target |
|---|---|
| `student_enrollments` | `(student_id, school_id)` → students; `(academic_year_id, school_id)` → academic_years; `(class_id, school_id)` → classes; `(section_id, school_id)` → sections; `(section_id, class_id)` → sections (the section must be in the class) |
| `student_parents` | `(student_id, school_id)` → students; `(parent_id, school_id)` → parents |
| `parents`, `students` | `(user_id, school_id)` → users (the login identity must belong to the same school) |

The services perform the same checks first, so callers get clear 404 or 422 errors. The
database is the backstop, and tests prove it with raw SQL inserts that bypass the services.

### Endpoints (`/api/v1`)

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/students` | `student.create` | Optional `password` creates the student login |
| GET | `/students` | any of `student.read`, `faculty.read_assigned_students`, `student.read_self`, `parent.read_child` | Filters: `status`, `admission_number`, `academic_year_id`, `class_id`, `section_id` |
| GET, PATCH | `/students/{id}` | read as above / `student.update` | `status` changes need `student.deactivate`; `admission_number` is immutable |
| GET | `/students/{id}/parents` | read as above | Scoped as in the table above |
| POST | `/students/{id}/parents` | `student.update` | Link a parent; duplicates return 409; a cross-school parent returns 422 |
| PATCH | `/students/{id}/parents/{parent_id}` | `student.update` | Change or (de)activate a link (never deleted) |
| POST | `/parents` | `user.create` | Creates the PARENT user (OTP sign-in) |
| GET | `/parents`, `/parents/{id}`, `/parents/{id}/children` | any of `user.read`, `parent.read_self` | A parent sees only themselves |
| PATCH | `/parents/{id}` | `user.update` | `is_active` needs `user.deactivate` |
| POST | `/enrollments` | `student.update` | `is_current`, `supersedes_enrollment_id` |
| GET | `/enrollments`, `/enrollments/{id}` | any of `student.read`, `faculty.read_assigned_students`, `student.read_self`, `parent.read_child` | Filters: `student_id`, `academic_year_id`, `class_id`, `section_id`, `current`, `status` |
| PATCH | `/enrollments/{id}` | `student.update` | Status, dates, number, `is_current` |

### Audit events

- **Students:** `student.created`, `.updated`, `.deactivated`, `.reactivated`.
- **Parents:** `parent.created`, `.updated`, `.deactivated`, `.reactivated`.
- **Links:** `student_parent.created`, `.updated`, `.deactivated`, `.reactivated`.
- **Enrollments:** `enrollment.created` (with any superseded id), `enrollment.updated`, and
  `enrollment.current_changed` (with the previous current id).
- **Sign-in:** student and parent sign-ins use the existing `auth.*` events. Student
  attempts are tagged `identifier_kind: "student"`.
- **Personal data** (date of birth, contact details, address, occupation) is audited by
  field name only (`{"field": "address", "redacted": true}`). Passwords, OTPs and tokens
  are never written; tests scan the database to prove it.

## Faculty and faculty assignments (Phase 5)

Phase 5 adds the faculty domain record and the assignments that define what each faculty
member teaches. Assignments are the **only** source of a faculty member's access to school
data.

### Faculty model and authentication

- `faculty` is a domain record. `faculty.user_id` is required, unique, and points at a
  user of the **same school** (composite FK `(user_id, school_id)` → users).
- Email, mobile and the password hash live on `users` only. There is no second password,
  JWT or session system: faculty sign in with the Phase 2 `POST /auth/login` (email or
  mobile + password), and Argon2id hashing is reused.
- `POST /faculty` creates the user, its `FACULTY` role and the faculty row in **one
  transaction** (a SAVEPOINT). A failure at any step (duplicate employee ID, email or
  mobile, password policy) leaves no orphaned user or faculty row. Alternatively,
  `user_id` attaches an existing, active FACULTY user of the same school.
- `employee_id` is trimmed, upper-cased (CHECK) and unique per school.
- `status` (`active`/`inactive`) drives a generated `is_active` column. Deactivating a
  faculty member deactivates their user, so sign-in, refresh and existing access tokens
  are all refused. Reactivation restores them. Nothing is deleted.
- Passwords are never returned or logged.

### Faculty assignment model

An assignment is **faculty + school + academic year + class + section + subject**, with
optional inclusive `effective_from` / `effective_to` dates inside the academic year.

- Placement columns are immutable. To change what someone teaches, deactivate the
  assignment and create a new one. History is kept, never deleted.
- Validation (clean 422 responses): the faculty member, year, class, section and subject
  must exist **in the caller's school** and be active, and the section must belong to the
  class. A record from another school gets the same "not found" answer as an unknown ID,
  so other schools' records cannot be probed. Reactivating an assignment re-runs these
  checks and requires an active faculty member.
- Co-teaching is allowed (two faculty members may share a placement), and one faculty
  member may hold many assignments.

### Database constraints

| Constraint | Enforces |
|---|---|
| `fk_faculty_assignments_faculty_same_school` `(faculty_id, school_id)` → faculty | faculty.school_id = assignment.school_id |
| `fk_faculty_assignments_year_same_school` `(academic_year_id, school_id)` → academic_years | academic_year.school_id = assignment.school_id |
| `fk_faculty_assignments_class_same_school` `(class_id, school_id)` → classes | class.school_id = assignment.school_id |
| `fk_faculty_assignments_section_same_school` `(section_id, school_id)` → sections | section.school_id = assignment.school_id |
| `fk_faculty_assignments_subject_same_school` `(subject_id, school_id)` → subjects | subject.school_id = assignment.school_id |
| `fk_faculty_assignments_section_in_class` `(section_id, class_id)` → sections | section.class_id = assignment.class_id |
| `uq_faculty_assignments_one_active` (partial unique, `WHERE status = 'active'`) | no duplicate *active* assignment; inactive history may repeat |
| `fk_faculty_user_same_school`, `uq_faculty_user_id`, `uq_faculty_school_id_employee_id` | one faculty record per user, same school; employee ID unique per school |

All foreign keys are `ON DELETE RESTRICT`. The composite targets on existing tables
(`UNIQUE (id, school_id)`, `UNIQUE (id, class_id)` on sections) already existed from 0004,
so migration 0005 creates two tables and modifies none. Raw-SQL tests that bypass the
services prove that every cross-school combination is rejected.

### Faculty scope and student visibility

A faculty member's scope is exactly their **effective** assignments: the assignment is
`active`, the faculty record is `active`, today's date **in the school's time zone** is
within the effective dates, and everything belongs to the caller's school.

| Resource | Visible to a faculty member when … |
|---|---|
| Students | the student has an **active** enrollment whose (year, class, section) equals an effective assignment's |
| Enrollments | the enrollment row itself is active and matches an assignment (other history rows stay hidden) |
| Academic years, classes, sections, subjects | some effective assignment names that record |
| Faculty | only their own record |
| Faculty assignments | only their own (including inactive history) |

Example: an assignment for 2026-27 / Class 10 / Section A / Mathematics reaches students
enrolled in 10-A for 2026-27 only. It does **not** reach 10-B, 9-A, another academic year
or another school. Access is never widened from a section to the whole class or school.

- Scope is applied **server-side as SQL conditions** to list, detail and filtered queries
  alike. Query filters (`section_id`, `class_id`, `academic_year_id`, `faculty_id`, …) are
  ANDed with the scope, so they can only narrow it. `GET /students?section_id=<unassigned>`
  returns an empty page, and an unassigned ID in a URL returns 404 (no existence leak).
- Faculty see students' names and admission data but **not** contact details or addresses,
  and never parent personal data. They have read-only access to student data.
- Moving a student to another section moves faculty access with them. Withdrawing the
  enrollment or deactivating the assignment or faculty member revokes it immediately.
- A user who is both FACULTY and PARENT gets the union of both scopes, and nothing more.
- Other roles keep Phase 3/4 behaviour. Staff with `student.read` stay school-wide.
- For later phases, `app.security.faculty_scope` exposes `faculty_can_access_student`,
  `faculty_can_access_section`, `faculty_can_access_class`, `faculty_can_access_subject`
  (optionally "this subject *in this section*") and `require_faculty_assignment_access`.

### Concurrency strategy

- Assignment create, update and reactivation lock the faculty row first
  (`SELECT … FOR UPDATE`), as faculty updates do. A faculty member's status and
  assignments therefore change one transaction at a time: an assignment racing a
  deactivation either commits first or sees the inactive faculty member and gets a 422.
- The partial unique index is the final authority. Writes flush inside a SAVEPOINT and
  constraint violations become a clean 409 or 422.
- Tests use real, separate PostgreSQL connections (no mocks) for duplicate assignments,
  competing distinct assignments, competing reactivation of history rows,
  deactivation-vs-update, assignment-waits-for-lock, duplicate employee ID and email, and
  a raw-SQL duplicate race.

### Endpoints (`/api/v1`)

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/faculty` | `faculty.manage` | Creates user + FACULTY role + faculty, or attaches `user_id` |
| GET | `/faculty`, `/faculty/{id}` | any of `faculty.manage`, `user.read`, `faculty.read_self` | A faculty member sees only themselves. Filters: `status`, `employee_id` |
| PATCH | `/faculty/{id}` | `faculty.manage` | `status` (de)activates the faculty member and their login |
| GET | `/faculty/me` | `faculty.read_self` | Own record |
| GET | `/faculty/me/assignments` | `faculty.read_self` | Own **effective** assignments (current scope) |
| POST | `/faculty-assignments` | `faculty.manage` | Duplicate active → 409; cross-school or invalid → 422 |
| GET | `/faculty-assignments`, `/faculty-assignments/{id}` | any of `faculty.manage`, `user.read`, `faculty.read_self` | Filters: `faculty_id`, `academic_year_id`, `class_id`, `section_id`, `subject_id`, `status` |
| PATCH | `/faculty-assignments/{id}` | `faculty.manage` | Effective dates and `status` only |

All list endpoints paginate (`page`, `page_size`). SUPER_ADMIN selects a school with
`X-School-ID`. Other users who name another school get 404.

### Audit events

- **Faculty:** `faculty.created`, `faculty.updated`, `faculty.deactivated`,
  `faculty.reactivated`. Email and mobile changes are recorded by field name only.
- **Assignments:** `faculty_assignment.created`, `.updated`, `.deactivated`,
  `.reactivated`. Each event carries the placement IDs (faculty, year, class, section,
  subject) and the school.
- No-op updates are not audited. Passwords, OTPs, tokens and secrets are never written;
  tests and the E2E run scan the stored audit metadata to prove it.

### Testing

- Integration tests run against real PostgreSQL: API, authorization and IDOR (swapped
  student, section, class, subject, academic-year, assignment and faculty IDs, plus
  filter widening and cross-school selection), scope helpers, raw-SQL constraints,
  concurrency, and migration 0005 upgrade → downgrade → re-upgrade with Phase 1–4 data
  preserved.
- **Mutation testing:** each protection was deliberately broken one at a time (student
  scope, section scope, cross-school validation in the service and in the database,
  duplicate protection in the index, the error mapping and the row lock). The suite
  caught 17 of 18 mutants. The survivor, dropping `enrollment.school_id = :school` from
  the student scope, is an *equivalent* mutant: `fk_enrollments_year_same_school` together
  with the assignment's same-school keys already forces the enrollment into the caller's
  school, so no valid database state can tell the difference. The condition stays as
  defence in depth.
- A real-HTTP E2E run against the Docker stack covers faculty creation and sign-in, self
  access, assigned and unassigned students, sections and subjects, cross-school and IDOR
  attacks, concurrent duplicate assignments, the deactivation race, and audit content.

## Attendance (Phase 6)

Phase 6 adds attendance: roll-call **sessions**, one **record** per student per session,
an append-only **change history**, corrections, bulk marking and summaries. It reuses
the existing authentication, RBAC, school scope, Phase 5 faculty assignment scope and
Phase 4 parent/student scope. There is no second authorization system.

### Data model

| Table | Holds |
|---|---|
| `attendance_sessions` | One roll call: school + academic year + class + section + school-local **date**, optional **subject**, `period` (1–20), optional `start_time`/`end_time`, `status` (`active`/`inactive`), `created_by_user_id` |
| `attendance_records` | One student's status in one session: `status`, immutable `original_status`, `remarks`, `marked_by`/`marked_at`, latest `corrected_by`/`corrected_at`/`correction_reason`, and the enrollment and placement the student held that day |
| `attendance_record_changes` | Append-only history: per-record `sequence` (1, 2, 3…), previous/new status, previous/new remarks, `reason`, `changed_by_user_id`, `changed_at` |

### Sessions: daily and subject roll calls

- `subject_id` **omitted** → a **daily** (general/class) roll call. `subject_id` set → a
  **subject** (period) roll call. `session_type` (`daily`/`subject`) is a generated
  column, so it can never disagree with `subject_id`. Schools can use either or both.
- `period` numbers roll calls of the same kind on the same day, so a double maths period
  or a morning/afternoon roll call is legitimate, not a duplicate.
- **What is a duplicate** (at most one *active* session each; inactive ones are history
  and may repeat):
  - daily: (academic year, section, date, period) where `subject_id IS NULL`
  - subject: (academic year, section, subject, date, period) where `subject_id IS NOT NULL`
  
  Both are partial unique indexes, so concurrent creates cannot both succeed.
- Validation: year, class, section and subject must exist in the school and be active; the
  section must belong to the class; the date must fall within the academic year and must
  not be in the future **in the school's time zone**.
- Placement, date, subject and period are immutable. `PATCH` changes times and `status`.
  An inactive session keeps its records, accepts no changes, and is left out of summaries
  and of parent/student views. Nothing is deleted.

### Statuses

`present`, `absent`, `late`, `excused`. Input is case-insensitive (`"PRESENT"` is stored as
`present`); anything else is rejected with 422, and a CHECK constraint enforces the same set
in the database.

### Who may do what

| Role | Read | Write |
|---|---|---|
| SUPER_ADMIN | the school selected with `X-School-ID` | yes |
| SCHOOL_ADMIN | own school | yes (incl. corrections) |
| MANAGEMENT | own school (incl. correction history) | no |
| FACULTY | sessions/records within their assignment scope (below) | subject roll calls of their assignments |
| PARENT | records and summaries of linked children | no |
| STUDENT | own records and summary | no |

The existing `attendance.read` / `attendance.manage` permissions already matched this, so
**no permission change was needed**. Parents and students never see sessions directly.

### Faculty authorization (the daily-attendance decision)

Faculty access comes only from *effective* Phase 5 assignments, and only for sessions
whose date lies inside the assignment's `effective_from`/`effective_to` window.

- **Subject sessions:** a faculty member reads *and* manages a subject session only with an
  assignment for the same **year + class + section + subject**. Math in 10-A does not
  reach 10-B, Physics in 10-A, 9-A, another year or another school.
- **Daily sessions: school staff only.** Phase 5 has no class-teacher concept, so letting
  any faculty member who teaches one subject in a section mark the whole section's daily
  attendance would widen their authority. Faculty can **read** the daily roll call of a
  section they teach, but cannot create or change it. A future class-teacher designation
  is the clean way to delegate daily roll calls.
- Faculty see records only through sessions they may read, never "all attendance of an
  assigned student". A refused create returns 403 *before* any record is looked up, so it
  reveals nothing about which IDs exist.

### Parent and student visibility

Parents see records of their linked children, and students their own. This reuses the
Phase 4 parent-child and self relationships (`people_scope.personal_student_visibility`).
Only *active* sessions are shown. Another child's or another student's records and
summaries return 404, and `?student_id=` filters can only narrow results.

### Eligibility and historical correctness

- A record can only be created for a student whose **enrollment placed them in the
  session's year, class and section on the session date**:
  - `enrollment_date <= date`;
  - for a *superseded* enrollment (moved mid-year), the date must be before the move date,
    since on the move date the student is already in the new section;
  - for other statuses, `end_date` is the last day (inclusive).
  
  So late joiners, leavers and moved students are handled from the enrollment history,
  never from the student's current placement.
- The record stores that enrollment and placement, and composite foreign keys tie it to
  both the session and the enrollment. When a student later moves to another section, their
  past attendance stays in the original section and session. Faculty who teach the new
  section do not inherit it.

### Bulk marking and completeness

- `POST /attendance/sessions/{id}/records/bulk` (up to 200 entries) is **atomic and
  create-only**. If any student is duplicated in the request, ineligible, or already marked,
  the whole request fails (422/409, with the offending indexes) and nothing is written.
- Marking may be **incomplete** (late joiners, students marked later).
  `GET /attendance/sessions/{id}/unmarked-students` lists eligible students without a
  record. Missing records are **never** treated as present or absent.

### Corrections and history

- `PATCH /attendance/records/{id}` changes `status` and/or `remarks`. A status change is a
  **correction**: it requires `reason` and sets `corrected_by`/`corrected_at`/
  `correction_reason`. `original_status` never changes.
- Every change, including remarks-only changes, appends a row to
  `attendance_record_changes` with a per-record `sequence`. The record's full history is at
  `GET /attendance/records/{id}/history`, readable by faculty in scope and school staff (not
  parents or students).
- The database enforces the history:
  - a status change without a reason violates a CHECK;
  - `UNIQUE (record_id, sequence)` stops two writers both appending change N;
  - a trigger rejects UPDATE of history rows, and DELETE unless a maintenance transaction
    sets `app.attendance_history_purge = 'on'`. This guards against accidents, not against
    the database owner.

### Summary calculation

`GET /attendance/students/{id}/summary` (filters: `academic_year_id`, `from_date`, `to_date`,
`subject_id`, `session_type`) returns counts per status plus:

```
attended            = present + late          (LATE counts as attended)
counted_sessions    = total_sessions - excused (EXCUSED is left out)
attendance_percentage = attended / counted_sessions * 100, 2 decimals; null if 0
```

Only marked records in active sessions are counted, and only those the caller may see. A
faculty member's summary therefore covers their own sessions. Unmarked sessions are not
counted. There is one implementation of this rule (`build_summary`).

### Time zone

A session's `session_date` (and its times) are **school-local**. "Today" for the
no-future-dates rule is computed by PostgreSQL in the school's IANA time zone, using the
same `school_today` helper that Phase 5 uses for assignment windows. It is never the
server's time zone. A test uses Pacific/Kiritimati (UTC+14) and Pacific/Pago_Pago (UTC−11),
whose dates always differ, to prove which clock is used.

### Concurrency strategy

- Every record write (single, bulk, correction) first locks the **session** row
  (`SELECT … FOR UPDATE`), then the record. Writes to one session are therefore serialised,
  and a session cannot be deactivated mid-write.
- Locked rows are **re-read** (`populate_existing`), so a correction always starts from the
  committed state, not a value read before the lock.
- The partial unique indexes (sessions), `UNIQUE (session_id, student_id)` (records) and
  `UNIQUE (record_id, sequence)` (history) are the final authority. Losers get a clean 409.
- Tests use real, separate PostgreSQL connections:
  - concurrent duplicate sessions (exactly one wins);
  - concurrent bulk submissions for the same students (one wins, complete), and disjoint
    ones (all succeed);
  - overlapping bulks (never half-written);
  - five concurrent corrections (a five-step consistent history);
  - deactivation racing bulk marking;
  - lock-wait proofs;
  - a raw-SQL duplicate race.

### Database constraints

| Constraint | Enforces |
|---|---|
| `fk_attendance_sessions_{year,class,section,subject}_same_school` | every session reference is in the session's school |
| `fk_attendance_sessions_section_in_class` | section.class_id = session.class_id |
| `uq_attendance_sessions_daily_active`, `uq_attendance_sessions_subject_active` | the duplicate rules above |
| `fk_attendance_records_session_placement` `(session_id, school_id, year, class, section)` → sessions | a record repeats its session's school and placement |
| `fk_attendance_records_enrollment_placement` `(enrollment_id, school_id, student_id, year, class, section)` → student_enrollments | the student held an enrollment in exactly that school/year/class/section |
| `uq_attendance_records_session_id_student_id` | one record per student per session |
| CHECKs | statuses, period range, time order, complete correction fields, reason required for a status change |
| `fk_attendance_record_changes_record_same_school`, `uq_attendance_record_changes_record_id_sequence`, append-only trigger | history integrity |

All foreign keys are `ON DELETE RESTRICT`. Raw-SQL tests that bypass the services prove
each rule.

### Endpoints (`/api/v1/attendance`)

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/sessions` | `attendance.manage` | Daily (staff) or subject (staff, or faculty in scope) |
| GET | `/sessions`, `/sessions/{id}` | `attendance.read` | Filters: `academic_year_id`, `class_id`, `section_id`, `subject_id`, `session_type`, `session_date`, `from_date`, `to_date`, `status` |
| PATCH | `/sessions/{id}` | `attendance.manage` | Times, `status` |
| POST | `/sessions/{id}/records` | `attendance.manage` | One student |
| POST | `/sessions/{id}/records/bulk` | `attendance.manage` | Atomic, create-only, ≤ 200 |
| GET | `/sessions/{id}/records` | `attendance.read` | Filter: `status` |
| GET | `/sessions/{id}/unmarked-students` | `attendance.read` | Eligible students without a record |
| GET | `/records`, `/records/{id}` | `attendance.read` | History. Filters: `student_id`, `academic_year_id`, `class_id`, `section_id`, `subject_id`, `session_type`, `status`, `session_date`, `from_date`, `to_date` |
| PATCH | `/records/{id}` | `attendance.manage` | Remarks or correction (`reason` required) |
| GET | `/records/{id}/history` | `attendance.manage` or `student.read` | Every change, in order |
| GET | `/students/{id}/summary` | `attendance.read` | Counts and percentage |

All lists paginate. Filters are ANDed with the caller's scope inside the SQL query, so they
only narrow results. Anything outside the scope returns 404, the same as an unknown ID.

### Audit events

- **Sessions:** `attendance_session.created`, `.updated`, `.deactivated`, `.reactivated`,
  each with the placement, date, subject and period.
- **Records:** `attendance_record.created`, `attendance_record.bulk_created` (count and
  per-status counts), `attendance_record.updated` (remarks) and
  `attendance_record.corrected`. Corrections carry the student, previous and new status,
  reason and history-row ID.
- **Remarks** may contain health or personal details, so they are recorded as
  `remarks_changed` only, never as text. Passwords, OTPs, tokens and secrets are never
  written.

### Migration `0006_attendance`

- Creates the three tables, their indexes, the composite keys and the append-only trigger.
- Adds `UNIQUE (id, school_id, student_id, academic_year_id, class_id, section_id)` to
  `student_enrollments` as a composite-FK target. `id` is already unique, so no existing row
  can violate it.
- Creates no rows. Upgrade → downgrade → re-upgrade preserves Phase 1–5 data (tested).

## Examinations, marks and report cards (Phase 7)

Phase 7 adds exams, their subject components, marks entry, corrections with an
append-only history, result calculation and published report cards. It reuses the
existing authentication, RBAC, school scope, Phase 5 faculty assignment scope and Phase 4
parent/student scope. There is no second authorization system, and **no permission or
role grant changed**.

### Data model

| Table | Holds |
|---|---|
| `exams` | school + academic year + **class**, optional **section** (`NULL` = every section of the class), `name`, `description`, `exam_type` (`unit_test`, `periodic`, `midterm`, `term`, `final`, `practical`, `other`), `start_date`/`end_date`, `status`, optional `pass_percentage` and `grade_bands`, creator, cancellation fields |
| `exam_subjects` | one component per subject per exam: `max_marks` (> 0, ≤ 1000), optional `pass_marks` (0 … max), optional `assessment_date` (within the exam) and times |
| `exam_marks` | one student's entry for one component: `status` (`present`/`absent`/`exempt`), `marks_obtained` (only when present), the component's `max_marks`, `remarks`, the enrollment and placement the student held, who entered it and when, and the latest correction (`corrected_by`/`corrected_at`/`correction_reason`) |
| `exam_mark_changes` | append-only correction history: per-mark `sequence`, previous/new status, marks and remarks, mandatory `reason`, `changed_by`, `changed_at` |
| `exam_publications` | one publication of an exam's results: `version` (1, 2, …), `status` (`active`/`withdrawn`), `settings` (the frozen exam configuration: names, components, maxima, passing marks, pass percentage, grade bands), who published/withdrew it, when and why |
| `exam_report_cards` | one student's result frozen at publication: totals, percentage, grade, result and one line per subject |

Marks, maxima and percentages are PostgreSQL `numeric` (marks `numeric(6,2)`), handled as
Python `Decimal`. Input with more than two decimals is rejected (never rounded), and
responses carry them as JSON numbers. Snapshot JSON stores decimals as strings, so no
floating-point value is ever stored.

### Exam lifecycle

```
draft ──schedule──▶ scheduled ──open-marks-entry──▶ marks_entry ──publish──▶ published
  ▲                    │                                ▲                       │
  └──return-to-draft───┘                                └────────reopen─────────┘
draft / scheduled / marks_entry ──cancel (reason)──▶ cancelled (terminal)
```

| Status | Exam fields | Subjects | Marks | Visible to parents/students |
|---|---|---|---|---|
| `draft` | all | add / change / remove | no | no |
| `scheduled` (needs ≥ 1 subject) | all | add / change | no | no |
| `marks_entry` | `pass_percentage`, `grade_bands` only | frozen | enter and correct | no |
| `published` | read-only | frozen | frozen | the active publication |
| `cancelled` | read-only | frozen | kept, frozen | no |

- Placement (year, class, section) is immutable. An exam's year, class and section must
  exist in the school and be active, the section must belong to the class, and the dates
  must lie inside the academic year.
- Exam names are unique per (year, class, section or whole class) among non-cancelled
  exams. A cancelled exam frees its name.
- One component per subject per exam (theory/practical splits are separate exams for now).
- Removing a subject (`DELETE`) is allowed only while the exam is **draft** and no mark
  references it. A scheduled exam must return to draft first, so it can never be left
  scheduled without subjects; from marks entry onwards configuration is frozen history.
  This is the only delete in the API. It is audited with the removed values, and it is
  enforced three times: the service, a trigger (`exam_subjects_removal_draft_only`) and
  `ON DELETE RESTRICT` from marks. Nothing published or historical can be deleted.
- Wrong transitions return `409`. A PostgreSQL trigger enforces the same transition table,
  so a buggy code path or raw SQL cannot skip a state.

### Who may do what

| Role | Configure exams and lifecycle | Enter / correct marks | Publish / reopen | Read |
|---|:-:|:-:|:-:|---|
| SUPER_ADMIN (selected school) | ✓ | ✓ | ✓ | everything in the school |
| SCHOOL_ADMIN | ✓ | ✓ | ✓ | everything in the school |
| MANAGEMENT | | | ✓ | everything in the school (incl. history and live results) |
| FACULTY | | within assignments | | exams of their classes; marks within assignments |
| PARENT | | | | published report cards of linked children |
| STUDENT | | | | own published report cards |

Permissions used: `marks.manage` (configuration, marks), `marks.publish` (publish, reopen),
`marks.read` (reads). They already existed with exactly the grants above, so the catalog
and the migration-seeded matrix are unchanged. Exam **configuration** additionally needs
school-wide scope: faculty hold `marks.manage` but get `403 "Exams are configured by school
staff."`. That is the same boundary as daily attendance in Phase 6.

### Marks entry workflow

1. Staff create a draft exam, add subjects, **schedule** it, then **open marks entry**.
2. Marks are entered per subject, for one student (`POST …/marks`) or many
   (`POST …/marks/bulk`, up to 200). `GET …/students?unmarked=true` lists eligible students
   without an entry.
3. Each entry is `present` (with `marks_obtained`, 0 … the subject's maximum), `absent` or
   `exempt`. Missing entries stay **pending**: never zero, absent or failed.
4. Staff review live results (`GET /exams/{id}/results`), then **publish**. Publishing is
   refused while entries are missing (`422` with counts) unless `allow_incomplete: true`;
   those report cards say `incomplete`.

**Eligibility.** A student may receive marks for a subject when an enrollment placed them
in the exam's year and class, and in the exam's section if it names one, **on the
assessment date**. The assessment date is the subject's `assessment_date`, or the exam's
`start_date` if none is set. The rule uses the same `enrollment_covers` logic as
attendance: late joiners only sit later assessments, and a student who moved section is
placed by the enrollment covering that date. The mark is pinned to that enrollment by a
composite foreign key.

**Bulk entry** is atomic and create-only. A duplicate student in the request, an
ineligible or out-of-scope student, marks above the maximum, or an already-entered student
rejects the whole request (`422`/`409`, with the offending indexes), and nothing is
written. Existing entries are changed with corrections.

### Faculty authorization

A faculty member enters, corrects and reads marks of a student for a subject only with an
**effective** Phase 5 assignment for the exam's **year + class + that student's section +
the subject**. The assignment must be:
- effective **today** (current authority, as everywhere else); and
- effective **on the assessment date** (the same rule attendance uses for session dates).

Example: maths in 10-A reaches 10-A students' maths marks only, not physics, 10-B, another
class, year or school. For a class-wide exam, a teacher of 10-A sees and enters only 10-A's
students. The scope is a SQL condition (`app.security.exam_scope`) applied inside list,
detail, eligibility and write queries alike, so filters only narrow. Swapped mark IDs
return 404. A student outside the teacher's sections gets the same "not eligible" answer
as an unknown ID.

Faculty see exams of classes/sections they teach, but they never configure or publish
exams and never read whole report cards. A report card spans every subject, and an
assignment covers one.

### Parent and student result visibility

Parents read report cards of their linked children, and students their own. This reuses
`people_scope.personal_student_visibility` (active link to an active parent / own student
record). They see **only the active publication**. Before publication, after a reopen
(withdrawal), and for everything else (exams, marks, live results, history, withdrawn
versions), the answer is an empty list or `404`. Parents and students hold no
`marks.manage`, so every write returns `403`.

### Corrections and history

- `PATCH /exam-marks/{id}` changes `status`, `marks_obtained` and/or `remarks`. **Every
  change is a correction and requires `reason`.** Switching to `absent`/`exempt` clears the
  marks. Switching to `present` requires `marks_obtained`. A no-op returns the mark
  unchanged and writes no history.
- Each correction appends a row to `exam_mark_changes` (previous and new status, marks and
  remarks, reason, who, when, numbered per mark) and updates `corrected_*`. The original
  value is the first row's `previous_*`. `GET /exam-marks/{id}/history` returns the history
  in order.
- Corrections are only possible in `marks_entry`. To correct a published result, reopen
  it first (below).
- The database enforces this: `reason` is `NOT NULL` and non-blank,
  `UNIQUE (mark_id, sequence)` stops two writers both appending change N, and a trigger
  rejects UPDATE and DELETE of history rows.

### Result calculation

One implementation, `app/services/result_calculation.py`, is used both for the live
preview and when publishing:

```
per subject:  present → marks_obtained / max_marks        absent → 0 / max_marks
              exempt  → left out of both totals            no entry → pending
total_obtained = Σ marks over present + absent subjects
total_max      = Σ max_marks over present + absent subjects   (each subject's own maximum)
percentage     = total_obtained / total_max × 100, rounded half-up to 2 decimals
                 (null if any subject is pending or total_max = 0)
```

Subject result: `pass` if `marks_obtained ≥ pass_marks`, otherwise `fail`. A subject
without passing marks is `not_applicable`. `absent`, `exempt` and `pending` are reported
as such.

Overall result, in order:
1. any subject pending → `incomplete`;
2. nothing counted, or no pass criteria at all (no subject has passing marks and the exam
   has no `pass_percentage`) → `not_applicable`;
3. a subject with passing marks is failed or absent, or `percentage < pass_percentage` →
   `fail`;
4. otherwise `pass`.

**Grades** are reported only when the exam has `grade_bands`
(`[{"grade": "A1", "min_percentage": 91}, …]`). Grades must be unique, thresholds must be
unique, and one band must start at 0. A percentage gets the band with the highest
threshold not above it. Grades apply overall and per present subject. No universal scale
is assumed: each exam defines its own, or reports no grades.

### Publication and report cards

Report cards are **snapshots**, never recalculated:
- **Publish** (`marks.publish`): the exam must be in `marks_entry`. Every student's result
  is calculated and stored in `exam_report_cards` under a new `exam_publications` row
  (version 1, 2, …). The exam configuration used is frozen in `settings`, so names, maxima,
  passing marks and grade bands are interpreted as they were. The exam becomes `published`.
- **Reopen** (`marks.publish`, `reason` required): the active publication becomes
  `withdrawn` (with who, when and why) and the exam returns to `marks_entry` for
  corrections. Parents and students stop seeing it at once.
- **Republish** creates the next version. Withdrawn versions stay readable by staff
  (`GET /exams/{id}/publications`, `GET /exams/{id}/report-cards?version=1`,
  `GET /report-cards/{id}`), unchanged, so a correction can never silently alter a
  published report card.
- A report card shows: school; academic year; class and section (the student's placement);
  exam (name, type, dates, pass percentage, grade bands); student (admission number,
  name); every subject with marks, maximum, passing marks, status, percentage, result and
  grade; totals, percentage, grade and result; publication version, status and time.
- The database enforces it: report cards can only be added to an active publication while
  the exam is being published, and can never be updated or deleted. A publication can
  only be withdrawn. `published` ⟺ an active publication exists (trigger), at most one
  active publication per exam (partial unique index).

PDF generation is not part of this phase. Report cards are served by the API.

### Concurrency strategy

- Configuration changes and lifecycle transitions lock the exam row (`FOR UPDATE`).
- Marks writes (single, bulk, correction) take a **share** lock on the exam row, so
  teachers entering different subjects do not block each other. Publishing and reopening
  take `FOR UPDATE` and therefore wait for in-flight marks writes. A marks write that was
  waiting then re-reads the exam (`populate_existing`), sees `published` and gets `409`.
  A correction racing a publication is therefore either in the snapshot or refused, never
  lost.
- Corrections additionally lock the mark row and re-read it before computing the change.
- `uq_exams_name_per_placement`, `UNIQUE (exam_id, subject_id)`,
  `UNIQUE (exam_subject_id, student_id)`, `UNIQUE (mark_id, sequence)`,
  `uq_exam_publications_one_active` and `UNIQUE (exam_id, version)` are the final
  authority. Losers get a clean `409`.

### Database constraints

| Constraint | Enforces |
|---|---|
| `fk_exams_{year,class,section}_same_school`, `fk_exams_section_in_class` | an exam's references are in its school; the section belongs to the class |
| `uq_exams_name_per_placement` (partial, `NULLS NOT DISTINCT`, non-cancelled) | no duplicate exam name per year/class/section |
| `fk_exam_subjects_{exam,subject}_same_school`, `uq_exam_subjects_exam_id_subject_id` | components are same-school and unique per subject |
| `fk_exam_marks_component` `(exam_subject_id, exam_id, school_id, max_marks)` → exam_subjects | a mark carries its component's exam, school and **maximum**; a referenced maximum cannot change |
| `fk_exam_marks_exam_placement` `(exam_id, school_id, year, class)` → exams | a mark repeats its exam's school, year and class |
| `fk_exam_marks_enrollment_placement` `(enrollment_id, school_id, student_id, year, class, section)` → student_enrollments | the student held an enrollment in exactly that school/year/class/section |
| `uq_exam_marks_exam_subject_id_student_id` | one entry per student per component |
| CHECKs | statuses, `0 ≤ marks ≤ max_marks`, marks present ⟺ status present, `0 < max ≤ 1000`, `0 ≤ pass ≤ max`, pass percentage 0–100, ordered dates/times, complete cancellation/correction/withdrawal fields, non-blank reasons |
| `fk_exam_mark_changes_mark_same_school`, `uq_exam_mark_changes_mark_id_sequence` | history integrity |
| `fk_exam_publications_exam_same_school`, `uq_exam_publications_exam_id_version`, `uq_exam_publications_one_active` | publication integrity |
| `fk_exam_report_cards_{publication,exam_placement,enrollment_placement}`, `uq_exam_report_cards_publication_id_student_id` | report cards belong to their publication, exam, school and enrollment |
| triggers `trg_exams_lifecycle`, `trg_exam_subjects_config`, `trg_exam_marks_guard`, `trg_exam_publications_guard`, `trg_exam_report_cards_insert`, `trg_exam_*_append_only` | transitions; configuration frozen outside draft/scheduled, removal only in draft, marks entry needs subjects; marks only in `marks_entry` and only for the exam's section; immutable placement; append-only history, publications and report cards |

All foreign keys are `ON DELETE RESTRICT`. Trigger violations carry a constraint name, so
they map to clean API errors like any other constraint. Deleting history or snapshot rows
needs `SET LOCAL app.exam_history_purge = 'on'` (a maintenance step for test data). It
guards against accidents, not against the database owner. Raw-SQL tests that bypass the
services prove each rule (`test_exam_constraints.py`).

### Endpoints (`/api/v1`)

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/exams` | `marks.manage` (staff) | Creates a draft |
| GET | `/exams`, `/exams/{id}` | `marks.read` | Filters: `academic_year_id`, `class_id`, `section_id`, `exam_type`, `status` |
| PATCH | `/exams/{id}` | `marks.manage` (staff) | Field rules per status (above) |
| POST | `/exams/{id}/schedule`, `/return-to-draft`, `/open-marks-entry` | `marks.manage` (staff) | Transitions |
| POST | `/exams/{id}/cancel` | `marks.manage` (staff) | `{"reason"}` |
| POST | `/exams/{id}/publish` | `marks.publish` | `{"allow_incomplete": false}` → publication (201) |
| POST | `/exams/{id}/reopen` | `marks.publish` | `{"reason"}` → withdrawn publication |
| GET | `/exams/{id}/results` | `marks.read` (staff) | Live calculation (paginated) |
| GET | `/exams/{id}/publications` | `marks.read` (staff) | All versions with counts |
| GET | `/exams/{id}/report-cards` | `marks.read` (staff) | Active publication, or `?version=` |
| POST, GET | `/exams/{id}/subjects` | manage (staff) / read | Components |
| GET, PATCH, DELETE | `/exams/{id}/subjects/{exam_subject_id}` | read / manage (staff) | PATCH in draft/scheduled; DELETE in draft only, without marks |
| GET | `/exams/{id}/subjects/{exam_subject_id}/students` | `marks.read` | Eligible students with their entry; `?unmarked=true` |
| POST | `/exams/{id}/subjects/{exam_subject_id}/marks` | `marks.manage` | One student |
| POST | `/exams/{id}/subjects/{exam_subject_id}/marks/bulk` | `marks.manage` | Atomic, create-only, ≤ 200 |
| GET | `/exams/{id}/subjects/{exam_subject_id}/marks` | `marks.read` | Filters: `student_id`, `status` |
| GET, PATCH | `/exam-marks/{id}` | read / manage | PATCH = correction (`reason` required) |
| GET | `/exam-marks/{id}/history` | `marks.read` | Every correction, in order |
| GET | `/report-cards`, `/report-cards/{id}` | `marks.read` | Published report cards in scope. Filters: `student_id`, `academic_year_id`, `exam_id` |

All lists paginate except publication history and mark history (small, bounded).
SUPER_ADMIN selects the school with `X-School-ID`. Anything outside the caller's scope
returns 404, the same as an unknown ID.

### Audit events

- **Exams:** `exam.created`, `exam.updated` (field diffs), `exam.scheduled`,
  `exam.returned_to_draft`, `exam.marks_entry_opened`, `exam.cancelled` (reason),
  `exam.published` (publication id, version, report-card and incomplete counts) and
  `exam.reopened` (withdrawn version, reason). Each includes the previous status where
  relevant.
- **Subjects:** `exam_subject.created`, `.updated` (diffs), `.deleted` (removed values).
- **Marks:** `exam_mark.created` (status, marks), `exam_mark.bulk_created` (count and
  per-status counts) and `exam_mark.corrected` (history-row id, sequence, previous and new
  status and marks, reason).
- Remarks may hold personal details, so they are recorded as `remarks_changed` only, never
  as text. Student names are not written to the audit log (IDs only). Passwords, OTPs and
  tokens are never written. The audit sanitiser redacts any key containing `pass`, so
  passing marks are audited as `qualifying_marks`.

### Migration `0007_examinations_marks`

- Creates the six tables, their indexes, composite keys and seven triggers.
- Uses only composite-FK targets that earlier migrations created; no existing table,
  column or row changes. Creates no rows.
- Upgrade → downgrade → re-upgrade preserves Phase 1–6 data, including attendance
  (`test_migration_0007.py`).

### Testing

- **Unit:** the result arithmetic (`test_result_calculation.py`: differing maxima,
  half-up rounding, exact decimals, pending vs zero vs absent vs exempt, pass marks,
  pass percentage, grade boundaries) and request validation (`test_exam_schemas.py`).
- **Integration (real PostgreSQL):** exam configuration and lifecycle
  (`test_exams_api.py`); single, bulk and corrected marks with faculty scope
  (`test_exam_marks_api.py`); results, publication, reopen/republish and visibility
  (`test_exam_results_api.py`); IDOR, cross-school, forged `X-School-ID` and filter attacks
  (`test_exam_authorization.py`); raw-SQL integrity for every constraint and trigger
  (`test_exam_constraints.py`); concurrency (`test_exam_concurrency.py`: duplicate exams,
  components racing the end of configuration, publish vs cancel, identical/disjoint/
  overlapping bulks, five concurrent corrections, duplicate publication and reopen,
  correction racing publication, lock-wait proofs, a raw-SQL duplicate race); migration
  preservation (`test_migration_0007.py`).
- **Mutation testing** (`backend/tools/mutation_exams.py`): 29 protections are broken
  one at a time. They cover faculty subject, section and date scope; exam visibility;
  parent-child and student-self scope; published and unpublished access; cross-school
  checks in the services and the database; maximum marks; duplicate marks; correction
  history; subject removal; lifecycle triggers; and locks. **27 are caught.**

  Two earlier survivors exposed real gaps, and dedicated tests now catch them:
  - publishing without the exam lock could miss a write it had waited for;
  - an inactive subject of another school could be told apart from an unknown one.

  The 2 remaining survivors are **equivalent** mutants:
  - dropping `school_id` from the eligibility query: the exam's academic year already pins
    the school through `fk_enrollments_year_same_school`;
  - dropping the pre-check for an existing mark: the unique constraint returns the same
    `409` with the same error location.
- **Real-HTTP E2E** (`backend/tools/e2e_exams.py`) against the rebuilt Docker stack:
  78/78 checks. They cover:
  - exam creation and the refusal for faculty and parents;
  - subject configuration, and subject removal refused once scheduled;
  - single, bulk and invalid marks, and bulk rollback;
  - corrections and history;
  - result calculation;
  - publication refused while marks are missing;
  - concurrent identical bulks and concurrent publication (exactly one wins);
  - a correction racing publication (the snapshot equals the committed mark);
  - parent and student access and denial;
  - unpublished-result denial;
  - cross-school, IDOR, forged `X-School-ID` and filter attacks;
  - reopen and republish with the version 1 snapshot preserved;
  - a scan of the run's stored audit rows (no password, token, OTP or student name).

### Verification tools (`backend/tools/`)

Developer tools, not part of the application or the pytest suite.

```bash
# Real-HTTP E2E against the running compose stack (from the repository root):
docker compose up -d --build
python backend/tools/e2e_exams.py --cleanup     # omit --cleanup to keep the data

# Mutation testing (from backend/, test database up):
python tools/mutation_exams.py --list
python tools/mutation_exams.py                  # or --only <text>
```

- `e2e_exams.py` uses the standard library only.
  - It refuses to run unless the backend reports `APP_ENV` `development` or `test`, and
    it needs `OTP_DELIVERY_BACKEND=development_console` (parent sign-in reads the
    development OTP code from the backend log).
  - It creates two fresh schools (`E2E7A-<tag>`/`E2E7B-<tag>`) and their staff through the
    admin CLI, with a random password passed by environment variable. The password is
    never printed and never put on a command line.
  - `--cleanup` deletes **only** the rows of the schools and users this run created (plus
    their audit rows), in one transaction. `login_attempts` rows hold only identifier
    hashes and are left to expire.
- `mutation_exams.py` only ever touches the **test** database, the same one pytest uses:
  `TEST_POSTGRES_DB`, default `<POSTGRES_DB>_test`. It refuses any name that does not end
  in `_test`.
  - Database mutants drop one constraint or trigger and rebuild the exam schema
    afterwards.
  - Code mutants edit one file at a time, after copying it to `<file>.mutation-backup`.
    An interrupted run is detected and restored on the next start, so a mutation can
    never be left in the source.

## Assignments and submissions (Phase 8)

Phase 8 adds coursework: assignments for a section and subject, student submissions with
an append-only revision history, deadlines, and reviews (feedback and optional grades)
with an append-only review history. It reuses the existing authentication, RBAC, school
scope, Phase 5 faculty assignment scope and Phase 4 parent/student scope. There is no
second authorization system.

### Data model

| Table | Holds |
|---|---|
| `assignments` | school + academic year + class + **section** + **subject**, `title`, `instructions`, `assigned_on` (school-local date), `due_at` (absolute instant), `allow_late`, `allow_resubmission`, optional `max_marks`, `status`, who created/published/closed it and when |
| `assignment_submissions` | one per student per assignment: the student and the enrollment placing them in the section, `revision_count`, first/last submission time, `is_late` (latest revision), and the review: `marks_awarded`, `feedback`, `reviewed_revision`, reviewer and time. `status` (`submitted`/`reviewed`) is a generated column |
| `assignment_submission_revisions` | append-only: every (re)submission's `content` (text, ≤ 20,000 characters), who, when, and the **due time at that moment**. `is_late` is a generated column (`submitted_at > due_at`) |
| `assignment_review_changes` | append-only: each review or review change, with previous/new marks and feedback, the revision reviewed, `reason` (required after the first), who, when |

**Attachments are not supported.** The platform has no file storage. There is no upload
handling, no `python-multipart` and no storage backend. Rather than adding an insecure or
unreviewed upload path, submissions are validated text. Secure attachments (type and size
limits, server-generated storage keys, authorized downloads) are a future extension.

### Lifecycle

```
draft ──publish (due_at in the future)──▶ published ──close──▶ closed
```

| Status | Editable | Visible to students/parents | Submissions | Reviews |
|---|---|---|---|---|
| `draft` | every field | no | no | no |
| `published` | title, instructions, `due_at`, `allow_late`, `allow_resubmission` | yes | yes | yes |
| `closed` | nothing | yes | no | yes |

- Placement (year, class, section, subject) is immutable.
- `max_marks` and `assigned_on` are fixed once published, so grades and authorization
  dates never shift under existing work.
- There is no reopening and no deletion.
- A trigger enforces all of this as well.

### Who may do what

| Role | Read | Create / edit / publish / close | Submit | Review / grade |
|---|---|:-:|:-:|:-:|
| SUPER_ADMIN (selected school), SCHOOL_ADMIN | the whole school | ✓ | | ✓ |
| MANAGEMENT | the whole school | | | |
| FACULTY | their section + subject | their section + subject | | their section + subject |
| STUDENT | own section's published work, own submissions | | own work | |
| PARENT | linked children's published work and submissions (not the submitted text) | | | |

**Faculty rule:** an *effective* Phase 5 assignment for the assignment's **year + class +
section + subject**. It must be effective **today** and **on `assigned_on`**, the same rule
as attendance session dates and exam assessment dates. Maths in 8-A does not reach
physics, 8-B, another class, year or school. The rule governs reading, managing and
reviewing alike, as one SQL condition inside every query. Filters only narrow, and other
IDs return 404.

**Permissions:** reads use `assignment.read`, and management and reviews use
`assignment.manage` (both existing). Students had no permission that fits handing in
work, and using a read permission for a write would blur the model. So **one permission
was added: `assignment.submit`, granted to STUDENT only.** It is persona-only, so
administrators do not get it. Migration 0008 seeds it with the deterministic id scheme of
migration 0002, and the RBAC test checks that the database matches the code. Grant
counts: STUDENT 14, every other role unchanged.

### Submissions, eligibility and deadlines

- **The submitter is always the caller.** A student hands in their own work; the request
  has no student field (`extra` fields are rejected).
- **Eligibility to submit:** the assignment is published and an enrollment places the
  student in its year, class and section **today in the school's time zone** (the same
  `enrollment_covers` rule as attendance and exams).
  - **Transfers:** a student who moves to another section after publication can no longer
    hand in work for the old section's assignments, but their earlier submissions stay
    readable.
  - A student who joins a section can submit its open assignments.
  - The submission is pinned to the enrollment it was made under (composite foreign key).
- **Visibility for students and parents:** published or closed assignments of a section
  the student was enrolled in during the assignment window (enrollment start on or before
  the due date, and no end before `assigned_on`), plus any assignment they have
  submitted to. Drafts are never visible.
- **Deadlines:** `due_at` must carry a time-zone offset; naive date-times are rejected,
  not guessed. Its school-local date must be on or after `assigned_on` and inside the
  academic year.
  - A (re)submission after `due_at` is **late**. It is refused (`409`) unless
    `allow_late`.
  - Every revision snapshots the due time, so moving the deadline later never rewrites
    whether earlier work was late. A trigger checks that the snapshot is honest and
    refuses late revisions when they are not allowed.
- **Resubmission** (`POST /assignment-submissions/{id}/revisions`) appends revision N+1
  and keeps every earlier one. It is allowed while the assignment is published,
  `allow_resubmission` is set, the student is still eligible, and the work has **not been
  reviewed**: a review is final for the student.
- **Missing work** is *outstanding* (`GET /assignments/{id}/students?outstanding=true`,
  status `not_submitted`). It is never treated as zero marks.

### Reviews and grading

- `PUT /assignment-submissions/{id}/review` records feedback and/or marks for the latest
  revision, by staff or faculty who manage the assignment, while it is published or
  closed.
- Marks need `max_marks` on the assignment (otherwise `422`, feedback only). They must be
  between 0 and the maximum, with at most two decimals; invalid input is rejected, never
  rounded. The database repeats the check (the submission carries the assignment's
  maximum through a composite foreign key).
- The first review needs no reason. **Every later change needs `reason`**. Each review is
  appended to `assignment_review_changes` with previous and new marks and feedback, who
  and when (`GET …/review-history`).
- Assignment grades are separate from exams. They never touch exam marks, results or
  published report cards (tested).

### Concurrency

- Every assignment write locks its row `FOR UPDATE`. (Re)submissions take a **share** lock
  on it, so closing waits for in-flight submissions, and a submission that waited then
  sees `closed` and gets `409`.
- Writes to one submission (resubmission, review) lock the submission row and re-read it.
  A resubmission racing a review therefore either lands first (and is the revision
  reviewed) or is refused because the work was reviewed.
- `UNIQUE (assignment_id, student_id)`, `UNIQUE (submission_id, revision)` and
  `UNIQUE (submission_id, sequence)` are the final authority. Losers get `409`.

### Database constraints

| Constraint | Enforces |
|---|---|
| `fk_assignments_{year,class,section,subject}_same_school`, `fk_assignments_section_in_class` | an assignment's references are in its school; the section belongs to the class |
| `fk_assignment_submissions_assignment_placement` `(assignment_id, school, year, class, section)` → assignments | a submission repeats its assignment's placement |
| `fk_assignment_submissions_enrollment_placement` `(enrollment_id, school, student, year, class, section)` → student_enrollments | the student held an enrollment in exactly that section |
| `fk_assignment_submissions_max_marks` + `ck_assignment_submissions_marks_range` | `0 ≤ marks ≤ the assignment's max_marks`; no marks without a maximum |
| `uq_assignment_submissions_assignment_id_student_id` | one submission per student per assignment |
| `uq_assignment_submission_revisions_submission_id_revision`, `uq_assignment_review_changes_submission_id_sequence` | gapless, non-duplicated histories |
| CHECKs | statuses, `0 < max_marks ≤ 1000`, complete publication/closure/review fields, content length, non-blank text, reason required after the first review |
| triggers `trg_assignments_lifecycle`, `trg_assignment_submissions_guard`, `trg_assignment_revisions_guard`, `trg_assignment_review_changes_append_only` | transitions, immutable placement, frozen grading settings, closed read-only, submissions only while published, honest due-time snapshots, late refusal, append-only histories |

All foreign keys are `ON DELETE RESTRICT`. Deleting submissions or history needs
`SET LOCAL app.assignment_history_purge = 'on'` (a maintenance guard against accidents,
not a security boundary). Raw-SQL tests prove each rule (`test_assignment_constraints.py`).

### Endpoints (`/api/v1`)

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/assignments` | `assignment.manage` | Creates a draft (faculty: own section + subject) |
| GET | `/assignments`, `/assignments/{id}` | `assignment.read` | Filters: `academic_year_id`, `class_id`, `section_id`, `subject_id`, `status` |
| PATCH | `/assignments/{id}` | `assignment.manage` | Field rules per status |
| POST | `/assignments/{id}/publish`, `/assignments/{id}/close` | `assignment.manage` | Transitions |
| GET | `/assignments/{id}/students` | `assignment.read` (staff, teaching faculty) | Roster with submission state; `?outstanding=true`; never through a parent or student link |
| POST | `/assignments/{id}/submissions` | `assignment.submit` | Student hands in own work |
| GET | `/assignment-submissions`, `/assignment-submissions/{id}` | `assignment.read` | Filters: `assignment_id`, `student_id`, `status`; parents get `content: null` |
| POST | `/assignment-submissions/{id}/revisions` | `assignment.submit` | Resubmission |
| GET | `/assignment-submissions/{id}/revisions` | `assignment.read` | Every revision (student, staff, faculty; not parents) |
| PUT | `/assignment-submissions/{id}/review` | `assignment.manage` | Feedback and/or marks; `reason` to change |
| GET | `/assignment-submissions/{id}/review-history` | `assignment.read` | Every review change |

All lists paginate except the two histories. SUPER_ADMIN selects the school with
`X-School-ID`. Anything outside the caller's scope returns 404.

### Audit events

- `assignment.created` (with whether it is graded), `assignment.updated` (field diffs;
  instructions by name only), `assignment.published` and `assignment.closed`.
- `assignment_submission.created` and `assignment_submission.resubmitted` (revision,
  lateness, **content length only**).
- `assignment_submission.reviewed` and `assignment_submission.review_changed`
  (history-row id, revision, previous and new marks, `feedback_changed`, reason).
- Submitted work and feedback text are not written to the audit log; the dedicated
  history tables hold them. Passwords, OTPs and tokens are never written; the E2E run
  scans the stored audit metadata to prove it.

### Migration `0008_assignments_submissions`

- Creates the four tables, their composite keys, indexes and four triggers.
- Adds the `assignment.submit` permission and its STUDENT grant. Downgrade removes them.
- No other existing table, column or row changes. Creates no domain rows.
- Upgrade → downgrade → re-upgrade preserves Phase 1–7 data (`test_migration_0008.py`).
  The 0005–0007 migration tests now compare data at their own revision before moving to
  head, because head legitimately adds a catalog row.

### Testing

- **Unit:** request validation (`test_assignment_schemas.py`: deadlines need a time-zone
  offset, marks and text limits, submission content, review input).
- **Integration (real PostgreSQL):** assignment creation, lifecycle, field rules per
  status and faculty scope (`test_assignments_api.py`); submissions, eligibility,
  transfers, deadlines and late rules, resubmission history, reviews, grades and review
  history, personal visibility and audit events (`test_assignment_submissions_api.py`);
  IDOR, cross-school, forged `X-School-ID`, filter attacks and dual-role users (a faculty
  member or a student who is also a parent) (`test_assignment_authorization.py`);
  raw-SQL integrity for every constraint and trigger (`test_assignment_constraints.py`);
  concurrency (`test_assignment_concurrency.py`: concurrent first submissions, concurrent
  resubmissions and review changes, resubmission racing a review, submission racing
  closure, concurrent publication, lock-wait proofs, a raw-SQL duplicate race); migration
  preservation (`test_migration_0008.py`). 93 Phase 8 tests.
- **Mutation testing** (`backend/tools/mutation_assignments.py`): 25 protections are
  broken one at a time. They cover faculty subject, section and date scope; draft
  visibility; parent-child and student-self scope; submission text and roster
  visibility for dual-role users;
  eligibility; ownership; deadlines; resubmission after review; grade limits; review
  reasons and history; review scope; duplicates; database constraints and triggers; and
  locks. **26 of 27 are caught.**

  Survivors found real gaps, and dedicated tests now catch them:
  - assignment visibility once matched *any* student's enrollment in the section;
  - the service checks for review scope and resubmission ownership looked redundant, but
    a user holding a parent role besides FACULTY or STUDENT can *see* a child's
    submission. Only these checks stop them reviewing work they do not teach, or adding
    revisions to someone else's work;
  - the roster and submitted text were granted to anyone *holding* the faculty role, so a
    faculty member who is also a parent saw the whole section's roster and their child's
    work for subjects they do not teach. Both now require staff reach over the
    assignment itself (`assignment_staff_reach`).

  The remaining survivor is **equivalent**: dropping the pre-check for an existing
  submission, because the unique constraint returns the same `409` with the same message.
- **Real-HTTP E2E** (`backend/tools/e2e_assignments.py`) against the Docker stack:
  57/57 checks. They cover:
  - creation refused for the wrong teacher, parents and students, naive due times and
    cross-school subjects;
  - drafts hidden from students and parents, and `max_marks` frozen once published;
  - submission, duplicate refusal, resubmission history and concurrent duplicate
    submissions (exactly one wins);
  - late work refused or accepted (and flagged) per `allow_late`;
  - grade validation, review scope, a reason required to change a grade, grade history,
    and resubmission refused after review;
  - parent and student access and denial (parents see grades but not the work);
  - cross-school, IDOR, forged `X-School-ID` and filter attacks;
  - closing (no new submissions, reviews still allowed);
  - report cards untouched by assignment grading;
  - a scan of the run's stored audit rows (no password, token or student name).

```bash
docker compose up -d --build
python backend/tools/e2e_assignments.py --cleanup   # from the repository root
python tools/mutation_assignments.py                # from backend/, or --only <text>
```

Both tools follow the same safety rules as the Phase 7 tools above.

## Health endpoints

| Endpoint                   | Purpose                                     | Codes     |
|----------------------------|---------------------------------------------|-----------|
| `GET /api/v1/health`       | App status + database connectivity check     | 200 / 503 |
| `GET /api/v1/health/live`  | Liveness only (no dependency checks)         | 200       |

## Security notes

- Secrets come only from environment variables. `.env` is git-ignored, and `.env.example`
  holds placeholders only.
- Production startup fails if `DEBUG=true`, the DB password or either key is a placeholder,
  Argon2 memory is below 19 MiB, or the OTP dev stub is enabled. Configuration errors never
  echo input values.
- Passwords are hashed with Argon2id (64 MiB, t=3, p=4 by default; rehashed automatically on
  login when the parameters change). The policy is length-based (10–128 characters), with a
  common-password blocklist and a rule against containing the user's own email, phone or
  name. There are no composition rules (NIST SP 800-63B).
- Login responses are generic, so they never reveal whether an account exists, is inactive
  or lacks a password. Unknown accounts take the same hashing time as real ones.
- Brute-force throttling allows 5 failures per identifier in 15 minutes (reset by a
  successful login), and 50 failures per IP. It is stored in PostgreSQL, so the limit holds
  across workers. Throttled requests return `429` with `Retry-After`.
- Token responses send `Cache-Control: no-store`. Identifiers used for throttling and OTP
  lookup are stored as HMAC fingerprints.
- The admin CLI refuses passwords on the command line (`allow_abbrev=False`, so `--password`
  cannot silently match `--password-env`).
- CORS origins must be listed explicitly; `*` is rejected.
- Security headers are set on every response. HSTS/TLS belong to the reverse proxy.
- The production image runs as a non-root user, with no `server` header.
- Compose ports are bound to `127.0.0.1` only.

## Known limitations

Phase 8:
- **No file attachments.** The platform has no secure file storage; submissions are
  text (≤ 20,000 characters). Attachments need a storage design first (allow-listed
  types, size limits, server-generated keys, authorized downloads, malware scanning).
- **No reopening.** A closed assignment stays closed; extend `due_at` (or set
  `allow_late`) before closing instead. Drafts cannot be deleted (there are no deletes).
- **A review is final for the student.** There is no "return for revision" workflow;
  staff can still change the review (with a reason).
- **Lateness is binary** (on time / late). There are no grace periods or late penalties;
  graders account for lateness in the marks.
- **Assignment grades are not aggregated** into any result or report card (by design for
  now; reporting is a later phase).
- **No notifications** for new work, deadlines or reviews (notifications phase).

Phase 7:
- **One component per subject per exam.** Theory/practical (or written/oral) splits of one
  subject need separate exams for now. Sub-components are a natural extension.
- **Grading is per exam.** Each exam carries its own optional grade bands and pass
  percentage. There is no school-wide grading-scheme catalog, no grade points/CGPA and no
  cumulative (term or annual) result across exams.
- **Absence policy is fixed.** An absent subject with passing marks fails; without passing
  marks it only lowers the percentage. Other policies (e.g. medical re-tests) would need a
  configurable rule.
- **Exam configuration is staff-only.** Faculty cannot create exams or add subjects.
  Delegated exam coordination would need a new designation (like the class-teacher idea in
  Phase 6).
- **No PDF report cards, ranks/positions, attendance on report cards, or per-report-card
  teacher remarks.** These belong to the reporting phase.
- **Student names on report cards are read live** (they are identity, not result data).
  Everything that determines the result is frozen in the snapshot.
- **No result notifications.** That belongs to the notifications phase.
- **History purge** (`app.exam_history_purge`) is a guard against accidents, not a
  security boundary against the database owner.

Phase 6:
- **Daily roll calls are recorded by school staff only.** There is no class-teacher concept
  yet, and letting any subject teacher of a section mark its daily attendance would widen
  their authority. A class-teacher designation (a later phase) is the natural extension.
- **No timetable link.** Sessions are created on demand (`period` numbers them); generating
  them from a timetable belongs to the timetable phase.
- **No attendance notifications** (e.g. absence alerts to parents). That belongs to the
  notifications phase, which can read records or audit events.
- **Summaries are per student only.** Section, class or school reports and dashboards belong
  to the reporting phase. Only marked records count; there is no "expected sessions"
  calendar, so a percentage reflects marked roll calls, not school days.
- **Holidays/school calendar are not modelled.** Staff simply do not create sessions;
  cancelled ones are deactivated.
- **Bulk marking is create-only.** Changing many records at once means one correction per
  record, by design, so every correction carries its own reason.
- **History purge** (`app.attendance_history_purge`) is a guard against accidents, not a
  security boundary against the database owner.

Phase 5:
- **No faculty password reset or change,** the same as for other accounts. The password
  can only be set at creation.
- **Faculty cannot be moved between schools.** A faculty record, its user and its
  assignments belong to one school.
- **Effective dates use the school's time zone at query time,** so access starts and ends
  at local midnight. There is no scheduled job; nothing needs one.
- **Assignment placement is immutable,** by design. Corrections are deactivate + create,
  which keeps history.
- **Subject-level scope is not used yet for student reads.** Student visibility follows the
  section, so a faculty member teaching any subject in 10-A sees 10-A's students. Later
  phases (attendance, marks) should call `faculty_can_access_subject(…, section_id=…)` or
  `require_faculty_assignment_access` for subject-specific writes.
- **Timetables, substitutes and class-teacher roles** are out of scope (later phases).

Phase 4:
- **No password reset or change.** A student's password can only be set when the student
  is created; a student created without one has no login until a reset workflow exists.
  This needs a later phase.
- **A parent belongs to one school.** `users.phone` is unique platform-wide and a user has
  one `school_id`, so a parent with children in two schools needs a future multi-school
  identity design.
- **Admission numbers can't be changed over the API,** because they are the login ID. A
  correction workflow, if needed, should be an audited admin operation.
- **Parents and students can't edit their own profiles,** and no self-service endpoints
  exist yet.
- ~~FACULTY see no student data~~ — resolved in Phase 5 (assignment-scoped access).
- **Graduating or transferring a student doesn't close their enrollments automatically.**
  Staff update enrollments explicitly.
- **`enrollment_number` is free text** and isn't unique.

Phase 3:
- **No HTTP endpoint to create schools.** Platform operators use
  `python -m app.cli create-school`. There is also no school, year, class, section or
  subject deletion, by design.
- **PARENT and STUDENT read their own school's structure** (years, classes, sections,
  subjects) through their existing `school.read` and `academic.read` permissions. This is
  non-personal data and is unchanged in Phase 4. Student and enrollment data is
  child- and self-scoped.
- ~~School timezone is stored but not yet used~~ — resolved: assignment windows (Phase 5)
  and attendance dates (Phase 6) use the school's time zone via `school_today`.
- **A failed `set-current` holds the school row lock until its transaction ends.** That is
  very short, but bulk operations on one school serialise.

Phase 2:

- ~~Faculty → assigned classes~~ — completed in Phase 5. Parent → child and student →
  self were completed in Phase 4.
- **No SMS provider.** OTP delivery is a development stub or disabled. A real provider
  should send asynchronously, through a queue, so response timing cannot reveal whether an
  account exists.
- **Refresh tokens are returned in the JSON body.** For the browser PWA (Phase 13), consider
  an `HttpOnly; Secure; SameSite=Strict` cookie for the refresh token.
- **Two concurrent refreshes with the same token** (for example, two browser tabs) are
  treated as reuse and end the session. This is deliberate for safety; the frontend should
  serialise refreshes.
- **No HTTP endpoints yet** for user CRUD, password change or reset, or 2FA. Accounts are
  created with the CLI.
- **No cleanup job** for expired sessions, OTP challenges and login attempts (planned for
  Phase 15). Global per-IP API rate limiting belongs at the reverse proxy (Phase 15).

## Roadmap

1. ✅ Foundation  2. ✅ Authentication + RBAC  3. ✅ School + academic structure
4. ✅ Students/parents/enrollments  5. ✅ Faculty + faculty assignments  6. ✅ Attendance
7. ✅ Exams + marks  8. ✅ Assignments  9. Timetable (not started)
10. Announcements + notifications  11. Reports + dashboards  12. Data import
13. Frontend portals  14. AI layer  15. Security hardening + deployment
