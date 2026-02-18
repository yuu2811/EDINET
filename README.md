# EDINET 大量保有モニター

EDINETから大量保有報告書・変更報告書をリアルタイムに検知し、Bloomberg端末風のWebダッシュボードで通知するシステム。

## 概要

```
┌─────────────────────────────────────────────────────┐
│              Web Dashboard (Browser)                │
│  ┌───────────┬──────────┬────────────────────────┐  │
│  │ Live Feed │ Watchlist│ Stats / Top Filers     │  │
│  │ (SSE)     │          │                        │  │
│  └───────────┴──────────┴────────────────────────┘  │
└───────────────────────┬─────────────────────────────┘
                        │ HTTP / SSE
┌───────────────────────┴─────────────────────────────┐
│              FastAPI Backend                         │
│  ┌───────────┬──────────┬────────────────────────┐  │
│  │ REST API  │ SSE      │ Background Poller      │  │
│  └───────────┴──────────┴────────────────────────┘  │
│  ┌───────────┬───────────────────────────────────┐  │
│  │ SQLite DB │ EDINET API Client + XBRL Parser   │  │
│  └───────────┴───────────────────────────────────┘  │
└───────────────────────┬─────────────────────────────┘
                        │
                  EDINET API v2
```

## 機能

- **リアルタイム通知**: Server-Sent Events (SSE) による即時プッシュ通知
- **ブラウザ通知**: Desktop Notification API によるデスクトップ通知
- **サウンドアラート**: Web Audio API による Bloomberg 風のアラート音
- **ウォッチリスト**: 特定銘柄の監視。該当する報告書が出た場合に特別通知
- **XBRL解析**: 保有割合・変動幅を自動抽出
- **Bloomberg風UI**: ダークテーマ、高密度表示、リアルタイムティッカー
- **フィルタリング**: 新規報告 / 変更報告 / 訂正 / 検索

## セットアップ

### 1. EDINET APIキーの取得

[EDINET 開示書類等閲覧ガイド](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html)からAPIキーを申請・取得してください。

### 2. 環境構築

```bash
# 依存パッケージのインストール
pip install -r requirements.txt

# 環境変数の設定
cp .env.example .env
# .env を編集して EDINET_API_KEY を設定
```

### 3. 起動

```bash
# 開発モード
python -m app.main

# または uvicorn で直接起動
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

ブラウザで http://localhost:8000 を開くとダッシュボードが表示されます。

## 設定

`.env` ファイルで以下の設定が可能です:

| 変数 | 説明 | デフォルト |
|------|------|------------|
| `EDINET_API_KEY` | EDINET API の Subscription Key | (必須) |
| `POLL_INTERVAL` | ポーリング間隔(秒) | `60` |
| `DATABASE_URL` | データベースURL | `sqlite+aiosqlite:///./edinet_monitor.db` |
| `HOST` | サーバーホスト | `0.0.0.0` |
| `PORT` | サーバーポート | `8000` |

## API

| エンドポイント | メソッド | 説明 |
|---|---|---|
| `GET /` | GET | ダッシュボード画面 |
| `GET /api/stream` | GET | SSE ストリーム |
| `GET /api/filings` | GET | 報告書一覧 (フィルタ対応) |
| `GET /api/filings/{doc_id}` | GET | 報告書詳細 |
| `GET /api/stats` | GET | 統計情報 |
| `GET /api/watchlist` | GET | ウォッチリスト取得 |
| `POST /api/watchlist` | POST | ウォッチリスト追加 |
| `DELETE /api/watchlist/{id}` | DELETE | ウォッチリスト削除 |
| `POST /api/poll` | POST | 手動ポーリング実行 |

## 技術スタック

- **Backend**: Python / FastAPI / SQLAlchemy / httpx
- **Frontend**: Vanilla HTML/CSS/JS (フレームワーク不使用)
- **Database**: SQLite (aiosqlite)
- **Real-time**: Server-Sent Events (SSE)
- **XBRL解析**: lxml
