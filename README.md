# EDINET 大量保有モニター

EDINETから大量保有報告書・変更報告書をリアルタイムに検知し、Bloomberg端末風のWebダッシュボードで通知するシステムです。個人投資家が機関投資家の大量保有動向をいち早く把握できることを目指しています。

## アーキテクチャ

```
┌──────────────────────────────────────────────────────────────────┐
│                    Web Dashboard (Browser)                        │
│  ┌──────────────┬───────────────┬─────────────────────────────┐  │
│  │ Live Feed    │ Stock Data    │ Stats / Summary              │  │
│  │ (SSE)        │ 時価総額/PBR  │ Top Filers / CSV Export      │  │
│  │ Date Nav     │ 52週チャート   │ Market Summary               │  │
│  │ Sort/Filter  │ Watchlist     │ Poll Countdown               │  │
│  ├──────────────┴───────────────┴─────────────────────────────┤  │
│  │ PC: テーブル/カード表示  │  Mobile: 専用3行カードレイアウト  │  │
│  └──────────┬──────────────┴──────────────────────────────────┘  │
└─────────────┼───────────────────────────────────────────────────┘
              │ HTTP REST / SSE
┌─────────────┴───────────────────────────────────────────────────┐
│                      FastAPI Backend                              │
│  ┌──────────────┬────────────┬──────────────┬─────────────────┐ │
│  │ REST API     │ SSE Stream │ Stock API    │ Background      │ │
│  │ 16 endpoints │ /api/stream│ stooq/Yahoo  │ Poller + XBRL   │ │
│  └──────────────┴────────────┴──────────────┴─────────────────┘ │
│  ┌──────────────┬─────────────────────────────────────────────┐ │
│  │ SQLite DB    │ EDINET Client + XBRL Parser + Demo Data     │ │
│  │ (aiosqlite)  │ XBRL ZIP → 保有割合・保有者・対象会社        │ │
│  └──────────────┴─────────────┬───────────────────────────────┘ │
└────────────────────────────────┼────────────────────────────────┘
                                 │ HTTPS
              ┌──────────────────┼──────────────────┐
              │                  │                  │
       EDINET API v2      stooq.com         Yahoo Finance
     (報告書/XBRL)       (株価履歴)        (時価総額/PBR)
                                          Google Finance
                                          Kabutan (株探)
```

## 機能一覧

### リアルタイム通知
- **SSE (Server-Sent Events)**: サーバーからブラウザへの即時プッシュ通知。新規報告書の検出時に自動でフィードに追加
- **デスクトップ通知**: Desktop Notification API による OS レベルの通知。クリックで該当報告書の詳細を表示
- **サウンドアラート**: Web Audio API による Bloomberg 風アラート音（660Hz: 通常、880Hz: ウォッチリスト一致時）
- **ティッカーバー**: 直近10件の報告書がスクロール表示される Bloomberg 風ティッカー
- **ポーリングカウントダウン**: ヘッダーに次回ポーリングまでの残り秒数をリアルタイム表示
- **提出数バッジ**: ヘッダーに当日の提出数をバッジ表示

### データ分析
- **XBRL自動解析**: EDINET の XBRL データから保有割合・前回保有割合・保有者名・対象会社名・証券コード・保有株数・保有目的を自動抽出
- **保有割合変動の自動計算**: 前回比の変動幅を算出し、増加(緑)/減少(赤)を色分け表示
- **報告書分類**: 新規報告(350)/訂正報告(360)/特例対象の自動判定
- **マーケットサマリー**: 当日の増加/減少件数、平均変動幅、最大変動銘柄を自動集計・表示
- **株価・時価総額表示**: 6つの無料データソース（stooq / Yahoo Finance / Google Finance / Kabutan）から株価・時価総額・PBR・PER・配当利回り・52週レンジ・出来高を取得。詳細モーダルに52週チャート付き
- **金融庁データ優先**: 企業名は Filing DB > EDINET コードリスト > 外部API > ハードコードの優先順位。発行済株式数は有報(120)/四半期(140)から取得し、最も正確な時価総額を算出
- **PDF プロキシ**: EDINET API v2 の Subscription-Key をブラウザに露出させず、サーバー側プロキシ経由で PDF を配信
- **doc_description からの企業名抽出**: XBRL未解析時でも `変更報告書（トヨタ自動車株式）` から対象企業名を自動抽出
- **XBRL再解析リトライ**: 未解析のfilingをローテーション方式で最大5件並行リトライ（30秒バッチタイムアウト付き）
- **保有割合の精度**: XBRL で decimal/percentage 形式を自動判定。Abstract/個別保有者の要素を除外し、Total（合計）保有割合のみを取得

### アナリティクス
- **提出者プロフィール**: 任意のEDINETコードで提出者の全履歴・対象企業一覧・活動サマリーを表示
- **対象企業プロフィール**: 証券コードで全大量保有報告・主要保有者・保有履歴チャートデータを表示
- **アクティビティランキング**: 7日/30日/90日/全期間で活発な提出者・注目銘柄・最大増減をランク表示
- **マーケットムーブメント**: 日次の買い/売り方向性分析・セクター別集計・注目変動銘柄
- **セクター分類**: TSE業種コード（33業種）に基づく自動分類・セクター別の件数/社数/平均保有割合

### 日付ナビゲーション
- **日付ピッカー**: カレンダーUIで任意の日付を選択
- **前日/翌日ボタン**: ワンクリックで日付を前後に移動
- **TODAYボタン**: 今日の日付に即座に戻る
- **FETCHボタン**: 選択した日付のデータをEDINETから取得（手動ポーリング）
- **過去データ閲覧**: 当日以外の過去の報告書データも閲覧可能

### フィルタリング・検索・ソート
- 報告種別フィルタ: 全件 / 新規報告 / 変更報告 / 訂正報告
- テキスト検索: 提出者名・対象会社名・報告書説明で絞り込み
- 日付範囲・証券コードによる API レベルのフィルタリング
- **ソート**: 新しい順 / 古い順 / 保有割合 高→低 / 保有割合 低→高 / 変動 大→小 / 変動 小→大

### CSVエクスポート
- **CSVダウンロード**: 表示中のフィード一覧をCSVファイルとしてエクスポート
- BOM付きUTF-8で出力（Excel での日本語文字化け防止）
- 提出日時、提出者名、対象会社、証券コード、保有割合、前回保有割合、変動幅、報告書種別を含む

### ウォッチリスト
- 特定銘柄（会社名・証券コード・EDINETコード）を登録
- ウォッチリスト銘柄に関連する報告書を即座に検出し、特別なアラート音(880Hz)で通知
- ウォッチリスト関連の報告書一覧を専用エンドポイントで取得

### UI
- **Bloomberg端末風ダークテーマ**: #0a0a0f 背景、アンバー/グリーン/レッドの配色
- **高密度表示**: モノスペースフォント、情報密度の高いカードレイアウト
- **左ボーダーによる色分け**: 新規報告(緑)・変更報告(アンバー)・訂正報告(紫)
- **カード背景グラデーション**: 保有割合が増加した報告書は緑グラデーション、減少は赤グラデーションでカード全体を着色
- **変動ピル**: 保有割合の変動幅を +/- 付きの色分けバッジで表示
- **保有割合バー**: 視覚的なプログレスバーで保有割合を直感的に表示
- **詳細モーダル**: 報告書の全フィールドを表示、EDINET原本・PDFへのリンク、保有割合ゲージ（前回比の視覚的比較）
- **キーボードナビゲーション**: モーダル表示中に左右矢印キーで前後の報告書を閲覧
- **マーケットサマリーパネル**: 増加/減少件数、平均変動、最大変動銘柄をサイドバーに表示
- **モバイル専用UI**: ヘッダー/ティッカー/サイドバーを非表示にし、専用の3行カードレイアウトで表示。保有割合・時価総額・PBR を一覧で確認可能
- **レスポンシブ対応**: PC（テーブル/カード表示）とモバイル（専用カード表示）を完全分離設計
- **PWA対応**: manifest.json によるホーム画面への追加対応

## セットアップ

### 前提条件

- Python 3.11 以上
- EDINET API の Subscription Key

### 1. EDINET APIキーの取得

[EDINET 開示書類等閲覧ガイド](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html)からAPIキーを申請・取得してください。

### 2. 環境構築

```bash
# リポジトリをクローン
git clone <repository-url>
cd EDINET

# 依存パッケージのインストール
pip install -r requirements.txt

# 環境変数の設定
cp .env.example .env
# .env を編集して EDINET_API_KEY を設定
```

### 3. 起動

```bash
# 開発モード (ホットリロード付き)
python -m app.main

# または uvicorn で直接起動
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

ブラウザで http://localhost:8000 を開くとダッシュボードが表示されます。

## Render へのデプロイ

本プロジェクトは Render の Free プランで動作するように構成されています。

### デプロイ手順

1. GitHub リポジトリを Render に接続
2. `render.yaml` がルートディレクトリにあることを確認（Blueprint として自動検出されます）
3. `EDINET_API_KEY` 環境変数を設定

### 注意事項

- Free プランでは永続ディスクが利用できないため、SQLite データベースは `/tmp` に配置されます
- デプロイのたびにデータベースは初期化されます（ポーラーが再取得します）
- `DATABASE_URL` は絶対パスで指定する必要があります: `sqlite+aiosqlite:////tmp/edinet_monitor.db`（スラッシュ4つ）

## 設定

`.env` ファイルで以下の設定が可能です:

| 変数 | 説明 | デフォルト | 必須 |
|------|------|------------|------|
| `EDINET_API_KEY` | EDINET API の Subscription Key | - | Yes |
| `POLL_INTERVAL` | ポーリング間隔（秒） | `60` | No |
| `DATABASE_URL` | SQLAlchemy データベース URL | `sqlite+aiosqlite:///./edinet_monitor.db` | No |
| `HOST` | サーバーバインドホスト | `0.0.0.0` | No |
| `PORT` | サーバーバインドポート | `8000` | No |

## API リファレンス

### SSE ストリーム

#### `GET /api/stream`

Server-Sent Events ストリーム。接続すると以下のイベントが配信されます:

| イベント名 | 発火タイミング | データ形状 |
|------------|---------------|-----------|
| `connected` | 接続直後 | `{"status": "connected"}` |
| `new_filing` | 新規報告書検出時 | Filing オブジェクト（後述） |
| `stats_update` | ポーリング完了時（新着あり） | `{"new_count": N, "date": "YYYY-MM-DD"}` |
| `: keepalive` | 30秒間イベントがない場合 | (コメント行、データなし) |

### 報告書 (Filings)

#### `GET /api/filings`

報告書一覧を取得。フィルタ・ページネーション対応。

**クエリパラメータ:**

| パラメータ | 型 | 説明 |
|-----------|------|------|
| `date_from` | string | 開始日 (`YYYY-MM-DD`) |
| `date_to` | string | 終了日 (`YYYY-MM-DD`) |
| `filer` | string | 提出者名で部分一致検索 |
| `target` | string | 対象会社名で部分一致検索 |
| `sec_code` | string | 証券コードで完全一致検索 |
| `amendment_only` | bool | 訂正報告のみ表示 (default: `false`) |
| `limit` | int | 取得件数 (1-500, default: `100`) |
| `offset` | int | オフセット (default: `0`) |

**レスポンス:**

```json
{
  "total": 42,
  "offset": 0,
  "limit": 100,
  "filings": [
    {
      "id": 1,
      "doc_id": "S100ABC1",
      "edinet_code": "E11111",
      "filer_name": "野村アセットマネジメント株式会社",
      "sec_code": "11110",
      "doc_type_code": "350",
      "doc_description": "大量保有報告書",
      "subject_edinet_code": "E22222",
      "issuer_edinet_code": "E22222",
      "holding_ratio": 6.25,
      "previous_holding_ratio": 5.10,
      "ratio_change": 1.15,
      "holder_name": "野村アセットマネジメント株式会社",
      "target_company_name": "ターゲット産業株式会社",
      "target_sec_code": "77770",
      "shares_held": 5000000,
      "purpose_of_holding": "純投資",
      "submit_date_time": "2026-02-18 09:15",
      "period_start": null,
      "period_end": null,
      "xbrl_flag": true,
      "pdf_flag": true,
      "is_amendment": false,
      "is_special_exemption": false,
      "created_at": "2026-02-18T09:20:00",
      "xbrl_parsed": true,
      "edinet_url": "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100...",
      "pdf_url": "/api/documents/S100ABC1/pdf"
    }
  ]
}
```

#### `GET /api/filings/{doc_id}`

指定した doc_id の報告書詳細を取得。Filing オブジェクトを返します。存在しない場合は `404` を返します。

### 統計情報

#### `GET /api/stats`

ダッシュボード用の統計情報を取得。

**クエリパラメータ:**

| パラメータ | 型 | 説明 |
|-----------|------|------|
| `date` | string | 対象日 (`YYYY-MM-DD`)。省略時は今日 |

**レスポンス:**

```json
{
  "date": "2026-02-18",
  "today_total": 15,
  "today_new_reports": 12,
  "today_amendments": 3,
  "total_in_db": 1250,
  "top_filers": [
    {"name": "野村アセットマネジメント株式会社", "count": 5},
    {"name": "ブラックロック・ジャパン株式会社", "count": 3}
  ],
  "connected_clients": 2,
  "poll_interval": 60
}
```

### ウォッチリスト

#### `GET /api/watchlist`

登録済みウォッチリスト一覧を取得。

```json
{
  "watchlist": [
    {
      "id": 1,
      "company_name": "トヨタ自動車",
      "sec_code": "72030",
      "edinet_code": "E02144",
      "created_at": "2026-02-18T08:00:00"
    }
  ]
}
```

#### `POST /api/watchlist`

ウォッチリストに銘柄を追加。

**リクエストボディ:**

```json
{
  "company_name": "トヨタ自動車",
  "sec_code": "72030",
  "edinet_code": "E02144"
}
```

- `company_name` は必須。`sec_code` と `edinet_code` は任意。

#### `DELETE /api/watchlist/{id}`

ウォッチリストから銘柄を削除。

#### `GET /api/watchlist/filings`

ウォッチリストに登録された銘柄に関連する報告書を最大50件取得。証券コード・EDINETコード・会社名でマッチングします。

### 株価データ

#### `GET /api/stock/{sec_code}`

指定した証券コードの株価・時価総額・PBR・52週チャートデータを取得。

**パラメータ:**

| パラメータ | 型 | 説明 |
|-----------|------|------|
| `sec_code` | string (path) | 4桁または5桁の証券コード（例: `7203`, `72030`） |

**レスポンス:**

```json
{
  "sec_code": "7203",
  "ticker": "7203.T",
  "name": "トヨタ自動車",
  "current_price": 2821.0,
  "market_cap": 44386200000000,
  "market_cap_display": "44兆3862億",
  "pbr": 1.2,
  "per": 10.5,
  "dividend_yield": 2.84,
  "week52_high": 3100.0,
  "week52_low": 2200.0,
  "price_change": 21.0,
  "price_change_pct": 0.75,
  "previous_close": 2800.0,
  "volume": 15000000,
  "price_source": "stooq",
  "weekly_prices": [
    {"date": "2026-02-21", "open": 2800, "high": 2850, "low": 2780, "close": 2821, "volume": 5000000}
  ]
}
```

**データソース（優先順位）:**

| 優先度 | ソース | 取得データ | コスト |
|--------|--------|-----------|--------|
| 1 | EDINET CompanyInfo (有報/四半期) | 発行済株式数・純資産 (金融庁データ) | 無料 |
| 2 | stooq.com | 株価履歴・現在値 (CSV API) | 無料 |
| 3 | Google Finance | 現在株価 (HTMLスクレイピング) | 無料 |
| 4 | Yahoo Finance | 時価総額・PBR・PER・配当利回り・52週レンジ | 無料 |
| 5 | Kabutan (株探) | 現在株価・社名・時価総額 (HTMLスクレイピング) | 無料 |
| 6 | フォールバック | 主要30銘柄の推定値 (全API不可時) | - |

**時価総額の算出:**
- 最優先: EDINET 発行済株式数 × ライブ株価（最も正確）
- 次善: Yahoo Finance 発行済株式数 × ライブ株価
- フォールバック: Yahoo Finance / Kabutan の時価総額値

**キャッシュ:** サーバー側30分 / クライアント側30分

### アナリティクス

#### `GET /api/analytics/filer/{edinet_code}`

提出者の全履歴・対象企業一覧・活動サマリーを取得。

#### `GET /api/analytics/company/{sec_code}`

特定の証券コードに対する全大量保有報告・主要保有者・保有履歴を取得。4桁/5桁コード対応。

#### `GET /api/analytics/rankings?period=30d`

期間別のアクティビティランキング。`period`: `7d` / `30d` / `90d` / `all`。活発な提出者Top10・注目銘柄Top10・最大増加/減少Top10・忙しい日Top5を返します。

#### `GET /api/analytics/movements?date=YYYY-MM-DD`

指定日のマーケットムーブメント分析。買い/売り方向性・セクター別集計・注目変動銘柄Top5を返します。

#### `GET /api/analytics/sectors`

TSE業種コードに基づくセクター別の件数・社数・平均保有割合の集計を返します。

### 手動ポーリング

#### `POST /api/poll`

バックグラウンドで即座にEDINETポーリングを実行。レートリミット: 10秒に1回。

**クエリパラメータ:**

| パラメータ | 型 | 説明 |
|-----------|------|------|
| `date` | string | ポーリング対象日 (`YYYY-MM-DD`)。省略時は今日 |

**レスポンス:**

```json
{"status": "poll_triggered", "date": "2026-02-18"}
```

**エラーレスポンス (429):**

```json
{"error": "Rate limited. Try again in 8s"}
```

## XBRL 解析の仕組み

EDINET から取得した XBRL ZIP ファイルを解析し、大量保有報告書の構造化データを抽出します。`local-name()` XPath を使用し、名前空間プレフィックスに依存しない堅牢なパースを行います。

### 抽出フィールドと検索パターン

| フィールド | 内容 | XBRL要素名パターン |
|-----------|------|-------------------|
| `holding_ratio` | 保有割合 (%) | `TotalShareholdingRatioOfShareCertificatesEtc`, `TotalShareholdingRatio`, `RatioOfShareholdingToTotalIssuedShares` (Abstract/EachLargeShareholder/JointHolder要素は除外。decimal形式(0.XX)は自動で%変換) |
| `previous_holding_ratio` | 前回保有割合 (%) | 同上（`contextRef` に `Prior`/`Previous` を含む要素） |
| `holder_name` | 報告義務発生者名 | `NameOfLargeShareholdingReporter`, `NameOfFiler`, `ReporterName`, `LargeShareholderName` |
| `target_company_name` | 発行者名 | `IssuerNameLargeShareholding`, `IssuerName`, `NameOfIssuer`, `TargetCompanyName` |
| `target_sec_code` | 対象証券コード | `SecurityCodeOfIssuer`, `IssuerSecuritiesCode`, `SecurityCode` |
| `shares_held` | 保有株式数 | `TotalNumberOfShareCertificatesEtcHeld`, `TotalNumberOfSharesHeld`, `NumberOfShareCertificatesEtc` |
| `purpose_of_holding` | 保有目的 | `PurposeOfHolding`, `PurposeOfHoldingOfShareCertificatesEtc` |

### 解析フロー

1. XBRL ZIP をダウンロード（30秒タイムアウト）
2. ZIP 内の `PublicDoc/*.xbrl` ファイルを特定
3. lxml で XML パース
4. 各フィールドについて複数の要素名パターンで検索
5. `contextRef` 属性で当期/前期を判別
6. パース結果を Filing レコードに反映

## バックグラウンドポーラーの動作

1. **起動時**: アプリケーション lifespan で `asyncio.create_task(run_poller())` として起動
2. **ポーリング**: `POLL_INTERVAL` 秒ごとに EDINET API v2 の `/documents.json` を呼び出し
3. **フィルタリング**: `docTypeCode` が `350`（大量保有報告書/変更報告書）または `360`（訂正報告書）の書類のみ抽出
4. **重複排除**: `doc_id` の一意制約で既存報告書をスキップ
5. **Document list enrichment**: `secCode`（発行体＝対象企業の証券コード）を `target_sec_code` にコピー。`doc_description` から対象企業名を正規表現で抽出
6. **XBRL enrichment**: `xbrl_flag=true` の場合、XBRL をダウンロード・解析して保有割合・保有目的等を付与
7. **XBRL再解析リトライ**: `xbrl_parsed=false` のfilingをローテーションオフセット方式で毎ポーリング時に最大5件並行リトライ。バッチ全体に30秒/個別に10秒のタイムアウトを設定し、ポーリング間隔の肥大化を防止。永続的に失敗するfilingが新しいfilingのリトライを阻害しないようオフセットを毎サイクル進行
8. **デモデータ自動生成**: EDINET API接続不可かつDB空の場合、25件のリアルなデモデータを自動投入
9. **リトライ**: EDINET API 呼び出し失敗時は指数バックオフ（2秒→4秒→最大30秒）で最大3回リトライ
10. **エラーハンドリング**: 個別の報告書ごとに try/except で処理。失敗時は `session.rollback()` して次へ
11. **SSE配信**: 新規報告書ごとに `new_filing` イベントを配信。ポーリング完了時に `stats_update` を配信
12. **シャットダウン**: `CancelledError` を捕捉して正常終了

## プロジェクト構造

```
EDINET/
├── app/
│   ├── __init__.py
│   ├── config.py            # 環境変数ベースの設定管理
│   ├── database.py          # SQLAlchemy async エンジン・セッション・DB初期化
│   ├── demo_data.py         # デモデータ生成（API接続不可時のフォールバック）
│   ├── edinet.py            # EDINET API v2 クライアント + XBRL パーサー
│   ├── errors.py            # グローバルエラーハンドラ登録
│   ├── logging_config.py    # ロギング設定
│   ├── main.py              # FastAPI アプリ (REST API + SSE + lifespan)
│   ├── models.py            # Filing / CompanyInfo / Watchlist ORM モデル
│   ├── schemas.py           # Pydantic スキーマ
│   ├── poller.py            # バックグラウンドポーラー + SSEBroadcaster + XBRLリトライ
│   └── routers/
│       ├── __init__.py
│       ├── analytics.py     # アナリティクス API（提出者/企業/ランキング/セクター）
│       ├── filings.py       # 報告書一覧・詳細 API + PDF プロキシ
│       ├── poll.py          # 手動ポーリング API（日付指定対応）
│       ├── stats.py         # 統計情報 API（日付指定対応）
│       ├── stock.py         # 株価API（stooq/Google Finance/Yahoo/Kabutan/フォールバック）
│       │                    #   PER・配当利回り・52週レンジ・出来高も取得
│       ├── stream.py        # SSE ストリーム
│       └── watchlist.py     # ウォッチリスト CRUD API
├── static/
│   ├── index.html           # ダッシュボード HTML
│   ├── icon.svg             # PWA アイコン
│   ├── manifest.json        # PWA マニフェスト
│   ├── css/
│   │   └── style.css        # Bloomberg風ダークテーマ CSS
│   └── js/
│       └── app.js           # フロントエンド JS (SSE・通知・日付ナビ・ソート・CSV・UI)
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # テストフィクスチャ・モックデータ
│   ├── test_api.py          # REST API エンドポイントテスト (18件)
│   ├── test_edinet.py       # EDINET クライアント・XBRL パーステスト (11件)
│   ├── test_models.py       # ORM モデルテスト (9件)
│   └── test_poller.py       # ポーラー・SSEブロードキャスターテスト (9件)
├── .env.example             # 環境変数テンプレート
├── .gitignore
├── Dockerfile               # Docker ビルド定義
├── pytest.ini               # pytest 設定 (asyncio_mode = auto)
├── README.md
├── render.yaml              # Render デプロイ設定
└── requirements.txt         # Python 依存パッケージ
```

## テスト

```bash
# テスト用依存パッケージのインストール
pip install pytest pytest-asyncio

# 全テスト実行 (49件)
pytest

# 詳細出力
pytest -v

# 特定テストファイルの実行
pytest tests/test_edinet.py
pytest tests/test_api.py
pytest tests/test_models.py
pytest tests/test_poller.py
```

テストではインメモリ SQLite (`sqlite+aiosqlite://`) を使用し、各テストが独立して実行されます。EDINET API 呼び出しは全てモックされるため、APIキーなしでテスト可能です。

## 技術スタック

| カテゴリ | 技術 |
|---------|------|
| Backend | Python 3.11+ / FastAPI 0.115 / SQLAlchemy 2.0 (async) |
| HTTP Client | httpx (async) |
| Database | SQLite (aiosqlite) |
| Real-time | Server-Sent Events (SSE) |
| XBRL Parser | lxml (XPath) |
| 株価データ | stooq.com / Google Finance / Yahoo Finance / Kabutan (株探)（6系統並列取得、全て無料・APIキー不要） |
| Frontend | Vanilla HTML / CSS / JavaScript (フレームワーク不使用) |
| UI Design | Bloomberg端末風ダークテーマ / モバイル専用カードレイアウト |
| Scheduler | asyncio ベースのポーリングループ |
| Testing | pytest / pytest-asyncio |
| Deploy | Render (Docker) |

## EDINET API について

本システムは [EDINET API v2](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html) を使用しています。

### 使用する docTypeCode

| コード | 種別 |
|--------|------|
| `350` | 大量保有報告書・変更報告書 |
| `360` | 訂正報告書（大量保有報告書） |

### 使用する docTypeCode（会社基本情報）

| コード | 種別 | 用途 |
|--------|------|------|
| `120` | 有価証券報告書 | 発行済株式数・純資産の取得 |
| `130` | 訂正有価証券報告書 | 同上（訂正版） |
| `140` | 四半期報告書 | 同上（四半期更新） |

### EDINET API v2 エンドポイント

- **書類一覧取得**: `GET /api/v2/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=...`
- **XBRL ダウンロード**: `GET /api/v2/documents/{docID}?type=1&Subscription-Key=...` (ZIP)
- **PDF ダウンロード**: `GET /api/v2/documents/{docID}?type=2&Subscription-Key=...` (PDF直接)
- **CSV ダウンロード**: `GET /api/v2/documents/{docID}?type=5&Subscription-Key=...` (ZIP, API v2新機能)
- **コードリスト**: `GET /api/v2/EdinetcodeDlInfo.csv?Subscription-Key=...` (cp932, Row 1はメタデータ)

### PDF プロキシ

ブラウザから直接 EDINET API を呼ぶと Subscription-Key が露出するため、サーバー側プロキシ経由で配信:

```
ブラウザ → GET /api/documents/{docID}/pdf → FastAPI プロキシ → EDINET API (type=2)
```

PDF取得失敗時はEDINET開示ページへ302リダイレクト。

## トラブルシューティング

### 外部APIに接続できない（403 Forbidden）

**原因**: 実行環境のネットワーク制限（プロキシ、ファイアウォール等）によりHTTPS通信がブロックされています。

**影響を受けるAPI:**

| API | 用途 | 必要なホスト |
|-----|------|-------------|
| EDINET API v2 | 報告書一覧・XBRL取得 | `api.edinet-fsa.go.jp` |
| stooq | 株価履歴・現在値 | `stooq.com` |
| Yahoo Finance | 時価総額・PBR・PER | `query1.finance.yahoo.com`, `query2.finance.yahoo.com` |
| Google Finance | 株価スクレイピング | `www.google.com` |
| Kabutan (株探) | 株価・社名スクレイピング | `kabutan.jp` |

**確認方法:**

```bash
# EDINET API接続テスト
curl -s -o /dev/null -w "%{http_code}" "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-02-19&type=2&Subscription-Key=YOUR_KEY"

# プロキシ設定の確認
env | grep -i proxy
```

**対処法:**

| 環境 | 対処 |
|------|------|
| **Claude Code（サンドボックス）** | 外部APIはホワイトリスト制で、金融APIは許可されていません。**mainにマージして本番環境（Render等）にデプロイ**すれば全て動作します |
| **社内ネットワーク** | プロキシ設定（`HTTP_PROXY` / `HTTPS_PROXY` 環境変数）を確認。必要に応じてIT部門に上記ホストの許可を依頼 |
| **Render / VPS / EC2** | 通常は制限なし。そのまま動作します |
| **Docker** | ネットワークモードを確認（`--network host` またはDNS設定） |

**フォールバック動作（API接続不可時）:**

- EDINET API接続不可 + DB空 → 25件のデモデータを自動生成
- 株価API全て接続不可 → 主要30銘柄の発行済株式数に基づく推定データを生成（2回目以降は即座にフォールバック）
- XBRL取得失敗 → `doc_description` から対象企業名を抽出、`secCode` を `target_sec_code` にコピー

### EDINET APIキーが正しいか確認したい

```bash
# APIキーの疫通テスト
curl -s "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=$(date +%Y-%m-%d)&type=2&Subscription-Key=YOUR_KEY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['metadata'])"
```

正常時: `{'status': '200', 'message': 'OK', ...}`

### 保有割合が「割合未取得」と表示される

**原因**: XBRL解析が完了していない。以下を確認:

1. **EDINET APIキーが設定されているか**: `.env` に `EDINET_API_KEY` が設定されているか確認
2. **ネットワーク接続**: XBRL ZIPダウンロード（`api.edinet-fsa.go.jp`）に接続できるか確認
3. **自動リトライ**: ポーラーが毎サイクル（デフォルト60秒）で未解析filingを最大5件リトライします。ネットワーク復旧後、自動的にデータが補完されます

### 時価総額が表示されない

**原因**: 株価APIへの接続が必要。stooq.com または Yahoo Finance への接続を確認してください。接続不可時は主要銘柄のみ推定値を表示します。

## ライセンス

MIT
