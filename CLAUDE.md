# CLAUDE.md — EDINET 大量保有モニター

Claude Code がこのリポジトリで作業するためのガイド。

## プロジェクト概要

EDINET API v2 から大量保有報告書をリアルタイム検知し、Bloomberg 風 Web ダッシュボードで通知するシステム。
FastAPI + SQLite(async) バックエンド、Vanilla JS フロントエンド。

## よく使うコマンド

```bash
# テスト全件実行（101件、約2秒）
python -m pytest tests/ -q

# 特定テストファイル
python -m pytest tests/test_api.py -q
python -m pytest tests/test_edinet.py -q
python -m pytest tests/test_poller.py -q
python -m pytest tests/test_models.py -q

# 開発サーバー起動
python -m app.main
# または
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## テスト

- **フレームワーク**: pytest + pytest-asyncio（`asyncio_mode = auto`）
- **DB**: インメモリ SQLite（各テスト独立）
- **外部API**: 全てモック済み（APIキー不要）
- **テストファイル**: `tests/test_api.py`, `test_edinet.py`, `test_poller.py`, `test_models.py`, `test_deps.py`, `test_database.py`
- **CI**: GitHub Actions（`.github/workflows/ci.yml`）— `main` と `claude/*` ブランチで自動実行

## アーキテクチャ — 主要ファイル

| ファイル | 役割 | 編集頻度 |
|---------|------|---------|
| `app/main.py` | FastAPI アプリ、REST API、SSE、lifespan | 中 |
| `app/poller.py` | バックグラウンドポーラー、SSEBroadcaster、XBRLリトライ | 中 |
| `app/edinet.py` | EDINET API v2 クライアント + XBRL パーサー | 低 |
| `app/models.py` | Filing / CompanyInfo / Watchlist ORM モデル | 低 |
| `app/routers/*.py` | APIエンドポイント（filings, stock, analytics, watchlist, etc.） | 中 |
| `static/js/app.js` | フロントエンド全体（SSE、通知、UI、ソート、フィルタ）— **約3400行** | 高 |
| `static/css/style.css` | Bloomberg風ダークテーマCSS | 低 |
| `static/index.html` | ダッシュボード HTML | 低 |

### フロントエンド注意点

- `static/js/app.js` は単一ファイルで約3400行。フレームワーク不使用の Vanilla JS
- PC（テーブル/カード表示）とモバイル（専用3行カード）で描画ロジックが分離している
- `prepareFilingData()` / `buildDocLinks()` が共通ヘルパー。レンダラー（`renderFilingsTable`, `renderFilingsCards`, `createMobileFeedCard`）から呼ばれる
- `toggleSound()` / `toggleNotify()` がデスクトップ・モバイル共通のトグルハンドラー
- 株価データは `stockCache[code]` でキャッシュ（クライアント30分、サーバー30分）

### バックエンド注意点

- `poller.py` の `_apply_pre_enrichment()` が document list 段階のエンリッチメント共通処理
- XBRL パースは `edinet.py` の `parse_xbrl()` — `local-name()` XPath で名前空間非依存
- 株価取得は `routers/stock.py` — stooq / Google Finance / Yahoo Finance / Kabutan の4ソース並列

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
