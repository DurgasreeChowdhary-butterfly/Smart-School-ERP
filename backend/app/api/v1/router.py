from fastapi import APIRouter

from app.api.v1 import (
    academic_years,
    assignments,
    attendance,
    auth,
    classes,
    enrollments,
    exams,
    faculty,
    faculty_assignments,
    health,
    parents,
    roles,
    school,
    sections,
    students,
    subjects,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(roles.router)
api_router.include_router(users.router)
api_router.include_router(school.router)
api_router.include_router(academic_years.router)
api_router.include_router(classes.router)
api_router.include_router(sections.router)
api_router.include_router(subjects.router)
api_router.include_router(students.router)
api_router.include_router(parents.router)
api_router.include_router(enrollments.router)
api_router.include_router(faculty.router)
api_router.include_router(faculty_assignments.router)
api_router.include_router(attendance.router)
api_router.include_router(exams.router)
api_router.include_router(assignments.router)
