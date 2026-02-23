import os
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

# Japan Standard Time (UTC+9) — shared across all modules
JST = timezone(timedelta(hours=9))


class Settings:
    EDINET_API_KEY: str = os.getenv("EDINET_API_KEY", "")
    EDINET_API_BASE: str = os.getenv(
        "EDINET_API_BASE", "https://api.edinet-fsa.go.jp/api/v2"
    )
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///./edinet_monitor.db"
    )
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # CORS: comma-separated allowed origins, or "*" for all (default)
    ALLOWED_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")
        if o.strip()
    ]

    # Stock data cache TTL in seconds (default: 30 minutes)
    STOCK_CACHE_TTL: int = int(os.getenv("STOCK_CACHE_TTL", "1800"))

    # Large shareholding report docTypeCodes
    LARGE_HOLDING_DOC_TYPES: list[str] = ["350", "360"]

    # Company fundamental data source docTypeCodes
    # 120: 有価証券報告書 (Annual Securities Report) — shares outstanding, net assets
    # 130: 訂正有価証券報告書 (Amended Annual Report)
    # 140: 四半期報告書 (Quarterly Report)
    COMPANY_INFO_DOC_TYPES: list[str] = ["120", "130", "140"]


settings = Settings()
