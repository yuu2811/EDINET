import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    EDINET_API_KEY: str = os.getenv("EDINET_API_KEY", "")
    EDINET_API_BASE: str = "https://api.edinet-fsa.go.jp/api/v2"
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///./edinet_monitor.db"
    )
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Large shareholding report docTypeCodes
    LARGE_HOLDING_DOC_TYPES: list[str] = ["350", "360"]

    # ordinanceCode for large shareholding
    LARGE_HOLDING_ORDINANCE: str = "060"

    # Company fundamental data source docTypeCodes
    # 120: 有価証券報告書 (Annual Securities Report) — shares outstanding, net assets
    # 130: 訂正有価証券報告書 (Amended Annual Report)
    # 140: 四半期報告書 (Quarterly Report)
    COMPANY_INFO_DOC_TYPES: list[str] = ["120", "130", "140"]


settings = Settings()
