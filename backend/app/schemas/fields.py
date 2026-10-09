"""Reusable, documented input normalisation for request schemas.

Normalisation rules (applied before validation):
* names/free text: surrounding whitespace is stripped; blank values are rejected;
* codes: stripped and upper-cased, then must match ``[A-Z0-9][A-Z0-9_-]*``;
* email: stripped and lower-cased; phone: converted to E.164.
Nothing else is altered.
"""

from __future__ import annotations

import re
import zoneinfo
from functools import lru_cache
from typing import Annotated, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, model_validator

from app.security.identifiers import normalize_phone

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_-]*$"


def _text(max_length: int) -> StringConstraints:
    return StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)


Text30 = Annotated[str, _text(30)]
Text50 = Annotated[str, _text(50)]
Text100 = Annotated[str, _text(100)]
Text150 = Annotated[str, _text(150)]
Text200 = Annotated[str, _text(200)]
Text500 = Annotated[str, _text(500)]
Text1000 = Annotated[str, _text(1000)]
PostalCode = Annotated[str, _text(20)]

_CODE_RE = re.compile(CODE_PATTERN)


def _code(value: str) -> str:
    # Runs after strip/upper-casing (a StringConstraints pattern would see the raw input).
    if not _CODE_RE.fullmatch(value):
        raise ValueError(
            "Must start with a letter or digit and contain only letters, digits, '-' or '_'."
        )
    return value


Code = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=30),
    AfterValidator(_code),
]


def _email(value: str) -> str:
    local, _, domain = value.partition("@")
    if not local or "." not in domain or " " in value:
        raise ValueError("Must be a valid email address.")
    return value


Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, min_length=3, max_length=320),
    AfterValidator(_email),
]


def _phone(value: str) -> str:
    normalized = normalize_phone(value)
    if normalized is None:
        raise ValueError("Must be in international format, e.g. +919876543210.")
    return normalized


Phone = Annotated[str, StringConstraints(min_length=8, max_length=32), AfterValidator(_phone)]


@lru_cache(maxsize=1)
def _timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def _timezone(value: str) -> str:
    if value not in _timezones():
        raise ValueError("Must be an IANA time zone name, e.g. Asia/Kolkata or UTC.")
    return value


Timezone = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_timezone),
]


def _https_url(value: str) -> str:
    if not value.startswith("https://") or len(value) <= len("https://") or " " in value:
        raise ValueError("Must be an https:// URL.")
    return value


HttpsUrl = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=2048), AfterValidator(_https_url)
]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatchInput(StrictInput):
    """Partial update: omitted fields are untouched; ``null`` only where documented.

    Subclasses list fields that may be cleared in ``nullable_fields``; sending
    ``null`` for any other field, or an empty body, is a validation error.
    """

    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _check_nulls_and_emptiness(self) -> PatchInput:
        provided = self.model_fields_set
        if not provided:
            raise ValueError("At least one field must be provided.")
        for name in provided:
            if getattr(self, name) is None and name not in self.nullable_fields:
                raise ValueError(f"{name} cannot be null.")
        return self

    def changes(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.model_fields_set}


_ADMISSION_RE = re.compile(r"^[A-Z0-9][A-Z0-9/_-]*$")


def _admission_number(value: str) -> str:
    # Validated after strip/upper-casing (see the Code type for why).
    if not _ADMISSION_RE.fullmatch(value):
        raise ValueError(
            "Must start with a letter or digit and contain only letters, digits, '/', '-' or '_'."
        )
    return value


AdmissionNumber = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=30),
    AfterValidator(_admission_number),
]

# Staff identifiers follow the same school-scoped identifier syntax as admission numbers.
EmployeeId = AdmissionNumber
