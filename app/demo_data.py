"""Generate realistic demo filings when EDINET API is unreachable.

This module seeds the database with plausible large shareholding filings
so the UI is fully functional even in environments where outbound HTTPS
is blocked.  The data is deterministic (same seed → same output) and
covers a variety of report types (new, change, amendment) with realistic
Japanese company names, securities codes, and holding ratios.
"""

import hashlib
import random as _random
from datetime import date, datetime, timedelta

from app.models import Filing

# ---------------------------------------------------------------------------
# Realistic Japanese market data
# ---------------------------------------------------------------------------

_TARGETS = [
    ("トヨタ自動車", "72030"),
    ("ソニーグループ", "67580"),
    ("キーエンス", "68610"),
    ("三菱UFJフィナンシャル・グループ", "83060"),
    ("ソフトバンクグループ", "99840"),
    ("東京エレクトロン", "80350"),
    ("日立製作所", "65010"),
    ("任天堂", "78740"),
    ("リクルートホールディングス", "60980"),  # 旧: 66980 は誤り
    ("信越化学工業", "40630"),
    ("ダイキン工業", "63670"),
    ("HOYA", "77410"),
    ("村田製作所", "69810"),
    ("伊藤忠商事", "80010"),
    ("三井住友フィナンシャルグループ", "83160"),
    ("ファーストリテイリング", "99830"),
    ("NTT", "94320"),  # NTTデータは2025-09上場廃止、NTT本体に変更
    ("KDDI", "94330"),
    ("アドバンテスト", "68570"),
    ("ディスコ", "61460"),
    ("レーザーテック", "69200"),
    ("メルカリ", "43850"),
    ("マネーフォワード", "39940"),  # 旧: 37180 は誤り
    ("SHIFT", "36970"),
    ("Sansan", "44430"),
    ("フリー", "44780"),  # 旧: 43780(CINC), 44780(freee) が正しい
    ("ラクスル", "43840"),  # 旧: 44670 は誤り
    ("ENECHANGE", "41690"),
    ("プレイド", "41650"),
]

_FILERS = [
    ("野村證券株式会社", "E03018"),
    ("三菱UFJモルガン・スタンレー証券", "E03010"),
    ("ブラックロック・ジャパン", "E15778"),
    ("JPモルガン・アセット・マネジメント", "E12345"),
    ("フィデリティ投信", "E10234"),
    ("大和証券グループ", "E03012"),
    ("ゴールドマン・サックス証券", "E03672"),
    ("三井住友DSアセットマネジメント", "E14983"),
    ("アセットマネジメントOne", "E12430"),
    ("日興アセットマネジメント", "E09327"),
    ("バンガード・グループ", "E17234"),
    ("ステート・ストリート", "E14298"),
    ("キャピタル・リサーチ", "E15832"),
    ("ウェリントン・マネジメント", "E16340"),
    ("タワー投資顧問", "E11982"),
    ("レオス・キャピタルワークス", "E25481"),
    ("スパークス・グループ", "E11673"),
    ("エフィッシモ キャピタル マネージメント", "E26178"),
    ("旧村上ファンド系", "E28341"),
    ("ストラテジックキャピタル", "E27654"),
]


def generate_demo_filings(
    target_date: date | None = None,
    count: int = 25,
    seed: int | None = None,
) -> list[Filing]:
    """Generate *count* realistic demo filing objects for *target_date*.

    Returns a list of unsaved Filing ORM instances.
    """
    if target_date is None:
        target_date = date.today()

    if seed is None:
        seed = int(hashlib.md5(target_date.isoformat().encode()).hexdigest()[:8], 16)

    rng = _random.Random(seed)

    filings: list[Filing] = []
    used_pairs: set[tuple[str, str]] = set()

    for i in range(count):
        target_name, target_code = rng.choice(_TARGETS)
        filer_name, filer_edinet = rng.choice(_FILERS)

        pair = (filer_edinet, target_code)
        if pair in used_pairs:
            continue
        used_pairs.add(pair)

        # Report type distribution: 60% change, 25% new, 15% amendment
        roll = rng.random()
        if roll < 0.60:
            doc_type = "350"  # 変更報告書
            is_amendment = False
            desc_prefix = "変更報告書"
        elif roll < 0.85:
            doc_type = "350"  # 大量保有報告書 (新規)
            is_amendment = False
            desc_prefix = "大量保有報告書"
        else:
            doc_type = "360"  # 訂正報告書
            is_amendment = True
            desc_prefix = "訂正報告書"

        # Holding ratio: typically 5-30%
        holding_ratio = round(rng.uniform(5.0, 35.0), 2)
        # Previous ratio: nearby (for change reports)
        if "変更" in desc_prefix:
            delta = round(rng.gauss(0, 2.5), 2)
            delta = max(-5.0, min(5.0, delta))
            previous_ratio = round(max(0, holding_ratio - delta), 2)
        else:
            previous_ratio = None if rng.random() < 0.4 else round(rng.uniform(3.0, holding_ratio), 2)

        # Submit time: business hours spread
        hour = rng.randint(8, 17)
        minute = rng.randint(0, 59)
        second = rng.randint(0, 59)
        submit_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            hour, minute, second,
        )

        doc_desc = f"{desc_prefix}（{target_name}株式）"
        doc_id = f"S{target_date.strftime('%Y%m%d')}{i:04d}"

        shares = rng.randint(500_000, 50_000_000)

        purposes = [
            "純投資",
            "政策投資",
            "経営参加",
            "純投資及び状況に応じて経営陣への助言",
            "保有割合の維持",
            "ポートフォリオ投資",
        ]

        filing = Filing(
            doc_id=doc_id,
            seq_number=i + 1,
            edinet_code=filer_edinet,
            filer_name=filer_name,
            sec_code=target_code,
            jcn=None,
            doc_type_code=doc_type,
            ordinance_code="030",
            form_code="030001" if not is_amendment else "030002",
            doc_description=doc_desc,
            subject_edinet_code=None,
            issuer_edinet_code=None,
            submit_date_time=submit_dt.strftime("%Y-%m-%d %H:%M"),
            period_start=None,
            period_end=target_date.isoformat(),
            xbrl_flag=True,
            pdf_flag=True,
            is_amendment=is_amendment,
            is_special_exemption=False,
            # XBRL-derived fields pre-populated
            holding_ratio=holding_ratio,
            previous_holding_ratio=previous_ratio,
            holder_name=filer_name,
            target_company_name=target_name,
            target_sec_code=target_code,
            shares_held=shares,
            purpose_of_holding=rng.choice(purposes),
            xbrl_parsed=True,
        )
        filings.append(filing)

    # Sort by submit time descending
    filings.sort(key=lambda f: f.submit_date_time or "", reverse=True)
    return filings
