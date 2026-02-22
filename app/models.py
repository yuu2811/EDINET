from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Filing(Base):
    """A large shareholding filing from EDINET."""

    __tablename__ = "filings"
    __table_args__ = (
        Index("ix_filings_submit_amendment", "submit_date_time", "is_amendment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    seq_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Filer info
    edinet_code: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    filer_name: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    sec_code: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    jcn: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Document classification
    doc_type_code: Mapped[str | None] = mapped_column(String(5), nullable=True)
    ordinance_code: Mapped[str | None] = mapped_column(String(5), nullable=True)
    form_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    doc_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Target company
    subject_edinet_code: Mapped[str | None] = mapped_column(
        String(10), nullable=True, index=True
    )
    issuer_edinet_code: Mapped[str | None] = mapped_column(
        String(10), nullable=True, index=True
    )

    # Extracted data from XBRL
    holding_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_holding_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    holder_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    target_company_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    target_sec_code: Mapped[str | None] = mapped_column(
        String(10), nullable=True, index=True
    )
    shares_held: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purpose_of_holding: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    submit_date_time: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    period_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    period_end: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Parent document (API v2 field #19: parentDocID)
    parent_doc_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # API v2 status fields (string "0"/"1"/"2" in API, stored as string)
    withdrawal_status: Mapped[str | None] = mapped_column(
        String(2), nullable=True
    )  # "0"=none, "1"=withdrawn, "2"=withdrawal of withdrawal
    disclosure_status: Mapped[str | None] = mapped_column(
        String(2), nullable=True
    )  # "0"=disclosed, "1"=not disclosed, "2"=non-disclosure notification
    doc_info_edit_status: Mapped[str | None] = mapped_column(
        String(2), nullable=True
    )  # "0"=no edit, "1"=edited, "2"=edit notification

    # Flags (API v2: all are string "0"/"1", stored as bool)
    xbrl_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    pdf_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    attach_doc_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    english_doc_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    is_amendment: Mapped[bool] = mapped_column(Boolean, default=False)
    is_special_exemption: Mapped[bool] = mapped_column(Boolean, default=False)

    # Internal
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    xbrl_parsed: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict:
        ratio_change = None
        if self.holding_ratio is not None and self.previous_holding_ratio is not None:
            ratio_change = round(
                self.holding_ratio - self.previous_holding_ratio, 2
            )

        return {
            "id": self.id,
            "doc_id": self.doc_id,
            "edinet_code": self.edinet_code,
            "filer_name": self.filer_name,
            "sec_code": self.sec_code,
            "doc_type_code": self.doc_type_code,
            "doc_description": self.doc_description,
            "subject_edinet_code": self.subject_edinet_code,
            "issuer_edinet_code": self.issuer_edinet_code,
            "holding_ratio": self.holding_ratio,
            "previous_holding_ratio": self.previous_holding_ratio,
            "ratio_change": ratio_change,
            "holder_name": self.holder_name,
            "target_company_name": self.target_company_name,
            "target_sec_code": self.target_sec_code,
            "shares_held": self.shares_held,
            "purpose_of_holding": self.purpose_of_holding,
            "submit_date_time": self.submit_date_time,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "xbrl_flag": self.xbrl_flag,
            "pdf_flag": self.pdf_flag,
            "english_doc_flag": self.english_doc_flag,
            "parent_doc_id": self.parent_doc_id,
            "withdrawal_status": self.withdrawal_status,
            "is_amendment": self.is_amendment,
            "is_special_exemption": self.is_special_exemption,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "xbrl_parsed": self.xbrl_parsed,
            # PDF proxy — tries EDINET API v2, then disclosure2dl,
            # then redirects to the EDINET viewer website.
            "pdf_url": f"/api/documents/{self.doc_id}/pdf"
            if self.doc_id
            else None,
            # Direct link to the EDINET viewer website
            "edinet_url": (
                f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx"
                f"?{self.doc_id},,,"
            )
            if self.doc_id
            else None,
        }


class CompanyInfo(Base):
    """Authoritative company fundamental data from EDINET filings.

    Populated from 有価証券報告書 (docTypeCode 120) and 四半期報告書 (140).
    These contain 発行済株式数 and 純資産 which are critical for accurate
    market cap and PBR calculation.

    Only the LATEST data for each sec_code is kept (upserted on each update).
    """

    __tablename__ = "company_info"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sec_code: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    edinet_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # 発行済株式数 (shares outstanding, from 有報/四半期報告書)
    shares_outstanding: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 純資産 (net assets, for PBR = price / BPS, BPS = net_assets / shares)
    net_assets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 業種 (industry sector from EDINET code list)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Source filing info
    source_doc_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_doc_type: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Period end date of the source filing (e.g. "2025-03-31")
    period_end: Mapped[str | None] = mapped_column(String(16), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict:
        bps = None
        if self.net_assets and self.shares_outstanding and self.shares_outstanding > 0:
            bps = round(self.net_assets / self.shares_outstanding, 2)
        return {
            "sec_code": self.sec_code,
            "edinet_code": self.edinet_code,
            "company_name": self.company_name,
            "shares_outstanding": self.shares_outstanding,
            "net_assets": self.net_assets,
            "bps": bps,
            "industry": self.industry,
            "source_doc_id": self.source_doc_id,
            "source_doc_type": self.source_doc_type,
            "period_end": self.period_end,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Watchlist(Base):
    """A company on the user's watchlist."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edinet_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sec_code: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    company_name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "edinet_code": self.edinet_code,
            "sec_code": self.sec_code,
            "company_name": self.company_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
