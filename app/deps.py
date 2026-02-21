"""Shared dependencies and utilities used across routers."""

from fastapi import HTTPException


def get_async_session():
    """Resolve async_session at runtime via app.main for testability."""
    import app.main
    return app.main.async_session


def normalize_sec_code(raw: str | None) -> str | None:
    """Normalize a securities code to its 4-digit form.

    5-digit codes have a trailing check digit which is stripped.
    Returns None for None/empty input.
    """
    if not raw:
        return None
    code = raw.strip()
    if len(code) == 5 and code[:4].isdigit():
        return code[:4]
    if len(code) == 4 and code.isdigit():
        return code
    return None


def validate_sec_code(sec_code: str) -> str:
    """Validate and normalize a securities code from user input.

    Raises HTTPException(400) for invalid codes.
    """
    result = normalize_sec_code(sec_code)
    if result is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid securities code: {sec_code!r} (expected 4 or 5 digit code)",
        )
    return result
