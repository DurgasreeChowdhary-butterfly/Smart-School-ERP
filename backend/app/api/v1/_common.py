"""Small helpers shared by v1 routers."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from app.api.deps import PaginationParams
from app.schemas.common import ErrorResponse, Page

ERRORS: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def to_page[S: BaseModel](
    schema: type[S], items: Sequence[object], total: int, pagination: PaginationParams
) -> Page[S]:
    return Page[schema].create(  # type: ignore[valid-type]
        [schema.model_validate(item) for item in items],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )
