# Smart School Platform

Centralized school management platform: parent, student, faculty and management portals.

**Status:** Phase 6 complete — attendance: daily and subject roll-call sessions,
present/absent/late/excused records, atomic bulk marking, corrections with an append-only
history, attendance summaries, and the same scoping as the rest of the platform (faculty
through their assignments, parents through linked children, students to themselves, staff
to their school). It builds on Phase 5 (faculty and assignments), Phase 4 (students,
parents, enrollments), Phase 3 (schools, academic structure), Phase 2 (authentication,
RBAC, audit) and Phase 1 (FastAPI, PostgreSQL, Alembic, Docker, tests).

> **Phase 7 (exams and marks) has NOT started.** Not implemented yet: exams, marks, report
> cards, assignments/submissions, timetable, announcements, notifications, fees, reports
> (beyond attendance summaries), AI and the frontend portals.

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
                       parents, enrollments, faculty, faculty_assignments
    core/              cross-cutting infrastructure: logging, errors, middleware,
                       request context (IP / user agent / request id)
    models/            ORM models: users, RBAC, auth sessions/tokens/OTP, audit log,
                       schools, academic years, classes, sections, subjects,
                       students, parents, student_parents, student_enrollments,
                       faculty, faculty_assignments
    schemas/           request/response models, shared envelopes
    services/          business logic: auth, OTP sign-in, RBAC, user provisioning,
                       school profile, academic structure, students, parents,
                       enrollments, faculty and faculty assignments
    repositories/      data access
    security/          passwords, JWT/refresh tokens, OTP, RBAC catalog,
                       principal, resource-scope framework, school scope,
                       people scope, faculty scope
    audit/             audit-trail writer with secret redaction
    utils/             small generic helpers
  alembic/             migrations
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

891 tests pass (167 of them added in Phase 6) with 99% coverage. Ruff format and lint
are clean and mypy strict reports no issues. Integration tests run against real
PostgreSQL, including:
- concurrency tests (`test_concurrency.py`, `test_people_concurrency.py`,
  `test_faculty_concurrency.py`, `test_attendance_concurrency.py`);
- IDOR tests for parents and students (`test_people_authorization.py`), for faculty
  (`test_faculty_authorization.py`) and for attendance (`test_attendance_authorization.py`);
- raw-SQL constraint tests (`test_*_constraints.py`);
- migration preservation tests (`test_migration_0003.py` to `test_migration_0006.py`).

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

Grant counts: SUPER_ADMIN 39, SCHOOL_ADMIN 37, MANAGEMENT 20, FACULTY 17, PARENT 14, STUDENT 13.

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
7. Exams + marks  8. Assignments  9. Timetable
10. Announcements + notifications  11. Reports + dashboards  12. Data import
13. Frontend portals  14. AI layer  15. Security hardening + deployment
