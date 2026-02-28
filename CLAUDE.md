# CLAUDE.md — EDINET 大量保有モニター

Claude Code がこのリポジトリで作業するためのガイド。

## プロジェクト概要

EDINET API v2 から大量保有報告書をリアルタイム検知し、Bloomberg 風 Web ダッシュボードで通知するシステム。
FastAPI + SQLite(async) バックエンド、Vanilla JS フロントエンド。

## よく使うコマンド

```bash
# テスト全件実行（164件、約10秒）
python -m pytest tests/ -q

# 特定テストファイル
python -m pytest tests/test_api.py -q
python -m pytest tests/test_edinet.py -q
python -m pytest tests/test_poller.py -q
python -m pytest tests/test_models.py -q
python -m pytest tests/test_stock.py -q

# 開発サーバー起動
python -m app.main
# または
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## テスト

- **フレームワーク**: pytest + pytest-asyncio（`asyncio_mode = auto`）
- **DB**: インメモリ SQLite（各テスト独立）
- **外部API**: 全てモック済み（APIキー不要）
- **テストファイル**: `tests/test_api.py`(52件), `test_edinet.py`(26件), `test_poller.py`(21件), `test_models.py`(17件), `test_stock.py`(21件), `test_deps.py`(25件), `test_database.py`(2件)
- **CI**: GitHub Actions（`.github/workflows/ci.yml`）— `main` と `claude/*` ブランチで自動実行

## アーキテクチャ — 主要ファイル

| ファイル | 役割 | 編集頻度 |
|---------|------|---------|
| `app/main.py` | FastAPI アプリ、REST API、SSE、lifespan | 中 |
| `app/poller.py` | バックグラウンドポーラー、SSEBroadcaster、XBRLリトライ、TOB検出、企業情報取得 | 中 |
| `app/edinet.py` | EDINET API v2 クライアント + XBRL パーサー（共同保有者・取得資金も抽出） | 低 |
| `app/models.py` | Filing / CompanyInfo / TenderOffer / Watchlist ORM モデル | 低 |
| `app/config.py` | 環境変数ベースの設定管理（TOB_DOC_TYPES 含む） | 低 |
| `app/routers/analytics.py` | アナリティクス・プロファイルAPI（タイムライン・TOBクロスリファレンス・企業情報） | 中 |
| `app/routers/filings.py` | 報告書一覧・詳細 API + PDF プロキシ | 低 |
| `app/routers/stock.py` | 株価API（stooq/Google/Yahoo/Kabutan）+ EDINETコードリスト（業種分類） | 中 |
| `app/routers/tob.py` | 公開買付（TOB）一覧 API | 低 |
| `app/routers/watchlist.py` | ウォッチリスト CRUD API | 低 |
| `static/js/app.js` | フロントエンド全体（SSE、通知、UI、ソート、フィルタ、プロファイル）— **約3700行** | 高 |
| `static/css/style.css` | Bloomberg風ダークテーマCSS（TOBパネル・プロファイルチャート含む） | 低 |
| `static/index.html` | ダッシュボード HTML（SSE再接続バナー・TOBパネル含む） | 低 |

### フロントエンド注意点

- `static/js/app.js` は単一ファイルで約3700行。フレームワーク不使用の Vanilla JS
- PC（テーブル/カード表示）とモバイル（専用3行カード）で描画ロジックが分離している
- `prepareFilingData()` / `buildDocLinks()` が共通ヘルパー。レンダラー（`renderFilingsTable`, `renderFilingsCards`, `createMobileFeedCard`）から呼ばれる
- `toggleSound()` / `toggleNotify()` がデスクトップ・モバイル共通のトグルハンドラー
- 株価データは `stockCache[code]` でキャッシュ（クライアント30分、サーバー30分）
- **プロファイル系関数**: `openFilerProfile()` / `openCompanyProfile()` がプロファイルモーダルを描画。`renderTimelineChart()` で SVG 推移チャート、`renderRelatedTobs()` で関連 TOB、`renderCompanyInfoPanel()` で企業基本情報を描画
- **SSE 再接続バナー**: `setConnectionStatus()` が `#sse-banner` の表示/非表示を制御

### バックエンド注意点

- `poller.py` の `_apply_pre_enrichment()` が document list 段階のエンリッチメント共通処理
- XBRL パースは `edinet.py` の `parse_xbrl()` — `local-name()` XPath で名前空間非依存。共同保有者（`_extract_joint_holders_xbrl`）と取得資金（`_matches_fund_source_pattern`）も抽出
- 株価取得は `routers/stock.py` — stooq / Google Finance / Yahoo Finance / Kabutan の4ソース並列
- XBRL リトライは `asyncio.Lock` で排他制御。コミットには30秒タイムアウトを設定
- 企業基本情報の `shares_outstanding` / `net_assets` にはバウンドチェック（異常値拒否）を実施
- TOB検出は `_poll_tender_offers()` — docTypeCode 240-300 をフィルタして TenderOffer モデルに保存
- プロファイルAPI（`analytics.py`）は `_build_timeline()` でチャート用時系列データ、`_fetch_related_tobs()` で関連TOBクロスリファレンス、`_fetch_company_info()` で企業基本情報を返却

## 環境変数（`.env`）

| 変数 | 必須 | デフォルト |
|------|------|-----------|
| `EDINET_API_KEY` | Yes | — |
| `POLL_INTERVAL` | No | `60` |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./edinet_monitor.db` |
| `HOST` / `PORT` | No | `0.0.0.0` / `8000` |
| `LOG_LEVEL` | No | `INFO` |

## 依存パッケージ

本番: `fastapi`, `uvicorn`, `sqlalchemy`, `aiosqlite`, `httpx`, `python-dotenv`, `lxml`
テスト: `pytest`, `pytest-asyncio`, `httpx`（テストクライアント用）

## デプロイ

- **Render**: `render.yaml` で Free プラン Docker デプロイ。DB は `/tmp` に配置（再デプロイでリセット）
- **Dockerfile** あり

## Claude Code 環境での制約事項

この環境（Claude Code サンドボックス）では以下の制限がある：

1. **外部APIアクセス不可**: EDINET API、stooq、Yahoo Finance 等の金融APIにはネットワーク制限でアクセスできない。テストは全てモック済みなので問題なし
2. **`gh` CLI が未インストール**: `apt install gh` で導入可能だが、GitHub API への認証情報がないため `gh pr create` は使えない。PRはブランチ push 後に Web UI から作成する
3. **git remote はローカルプロキシ経由**: `git push -u origin <branch>` は動作する。push 時にリモートからPR作成URLが表示されるのでそれを案内する

### PR作成の手順

```bash
# 1. ブランチをpush
git push -u origin claude/branch-name-xxx

# 2. push出力に表示されるURLをユーザーに案内
# remote: Create a pull request for '...' on GitHub by visiting:
# remote:      https://github.com/yuu2811/EDINET/pull/new/...
```

## コーディング規約

- Python: 型アノテーションあり、async/await ベース
- JS: Vanilla JS、`let`/`const` 使用、jQuery 不使用
- テストは変更したら必ず `python -m pytest tests/ -q` で全件パスを確認
- コミットメッセージ: `fix:`, `feat:`, `refactor:`, `test:`, `docs:` プレフィックス推奨
