from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import Pagination, PaginationParams
from app.schemas.common import Page


@pytest.mark.parametrize(
    ("page", "page_size", "offset"),
    [(1, 20, 0), (2, 20, 20), (5, 10, 40)],
)
def test_pagination_offset(page: int, page_size: int, offset: int) -> None:
    params = PaginationParams(page=page, page_size=page_size)
    assert params.offset == offset
    assert params.limit == page_size


@pytest.mark.parametrize(
    ("total", "page_size", "pages"),
    [(0, 20, 0), (1, 20, 1), (20, 20, 1), (21, 20, 2)],
)
def test_page_computes_page_count(total: int, page_size: int, pages: int) -> None:
    page = Page[int].create([], total=total, page=1, page_size=page_size)
    assert page.pages == pages


def _pagination_client(app: FastAPI) -> TestClient:
    @app.get("/_test/paginated")
    def paginated(params: Pagination) -> dict[str, int]:
        return {"page": params.page, "page_size": params.page_size}

    return TestClient(app)


def test_pagination_query_defaults(app: FastAPI) -> None:
    response = _pagination_client(app).get("/_test/paginated")
    assert response.json() == {"page": 1, "page_size": 20}


@pytest.mark.parametrize("query", ["page=0", "page_size=0", "page_size=101", "page=abc"])
def test_pagination_rejects_invalid_values(app: FastAPI, query: str) -> None:
    response = _pagination_client(app).get(f"/_test/paginated?{query}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
