"""Shared dependencies and utilities used across routers."""

import re

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


_EDINET_CODE_RE = re.compile(r"^E\d{5}$")


def validate_edinet_code(edinet_code: str) -> str:
    """Validate an EDINET code from user input.

    EDINET codes follow the format 'E' + 5 digits (e.g. E12345).
    Raises HTTPException(400) for invalid codes.
    """
    code = edinet_code.strip()
    if not _EDINET_CODE_RE.match(code):
        raise HTTPException(
            status_code=400,
            detail=f"無効なEDINETコードです: {edinet_code!r} (例: E12345)",
        )
    return code


_DOC_ID_RE = re.compile(r"^S[A-Za-z0-9]{7,12}$")


def validate_doc_id(doc_id: str) -> str:
    """Validate an EDINET document ID from user input.

    Document IDs follow the format 'S' + 7-12 alphanumeric chars (e.g. S100ABC1).
    Raises HTTPException(400) for invalid IDs.
    """
    did = doc_id.strip()
    if not _DOC_ID_RE.match(did):
        raise HTTPException(
            status_code=400,
            detail=f"無効な書類IDです: {doc_id!r}",
        )
    return did
