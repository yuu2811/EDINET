"""Pydantic schemas for request/response validation."""

from pydantic import BaseModel, Field


class WatchlistCreate(BaseModel):
    """Schema for adding a company to the watchlist."""

    company_name: str = Field(..., min_length=1, max_length=256)
    sec_code: str | None = Field(None, max_length=10)
    edinet_code: str | None = Field(None, max_length=10)


class WatchlistResponse(BaseModel):
    """Schema for a single watchlist item response."""

    id: int
    company_name: str
    sec_code: str | None = None
    edinet_code: str | None = None
    created_at: str | None = None


class PollResponse(BaseModel):
    """Schema for poll trigger response."""

    status: str
