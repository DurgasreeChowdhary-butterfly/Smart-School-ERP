from __future__ import annotations

from app.audit.service import REDACTED, sanitize_metadata


def test_sensitive_keys_are_redacted_recursively() -> None:
    result = sanitize_metadata(
        {
            "password": "hunter2",
            "new_password": "x",
            "refresh_token": "abc",
            "Authorization": "Bearer abc",
            "code": "123456",
            "otp": "123456",
            "client_secret": "s",
            "code_hash": "h",
            "nested": {"access_token": "t", "reason": "invalid_password"},
            "items": [{"password": "p"}],
            "reason": "ok",
            "attempts": 3,
        }
    )
    for key in (
        "password",
        "new_password",
        "refresh_token",
        "Authorization",
        "code",
        "otp",
        "client_secret",
        "code_hash",
    ):
        assert result[key] == REDACTED
    assert result["nested"] == {"access_token": REDACTED, "reason": "invalid_password"}
    assert result["items"] == [{"password": REDACTED}]
    assert result["reason"] == "ok"
    assert result["attempts"] == 3


def test_non_json_values_are_stringified_and_depth_limited() -> None:
    deep: dict[str, object] = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    result = sanitize_metadata({"obj": object.__name__, "set": {1}, "deep": deep})
    assert result["set"] == [1]
    assert REDACTED in str(result["deep"])
