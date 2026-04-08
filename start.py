#!/usr/bin/env python3
"""
Claude Code 使用量ダッシュボード
ローカルのClaude Codeログを解析して、使用量をブラウザで可視化します。

使い方:
  python3 start.py              # スキャン → サーバー起動 → ブラウザ自動オープン
  python3 start.py --port 3000  # ポート指定
  python3 start.py --no-browser # ブラウザを自動で開かない
  python3 start.py --rescan     # 全ファイル再スキャン（キャッシュ無視）
"""

import argparse
import signal
import sys
import threading
import webbrowser

from scanner import init_db, scan
from server import start_server


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code 使用量ダッシュボード",
    )
    parser.add_argument("--port", type=int, default=8080, help="サーバーポート（デフォルト: 8080）")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    parser.add_argument("--rescan", action="store_true", help="全ファイルを再スキャン")
    args = parser.parse_args()

    print("=" * 50)
    print("  Claude Code 使用量ダッシュボード")
    print("=" * 50)
    print()

    # DB初期化 & スキャン
    print("データベースを初期化中...")
    conn = init_db()

    print("ログファイルをスキャン中...")
    scanned, turns = scan(conn, force=args.rescan)
    print(f"  {scanned} ファイル処理、{turns} ターン追加")

    # サマリー
    row = conn.execute(
        "SELECT COUNT(*) as s, COALESCE(SUM(turn_count),0) as t, COALESCE(SUM(total_cost),0) as c FROM sessions"
    ).fetchone()
    print(f"  累計: {row['s']} セッション / {row['t']} ターン / ${row['c']:.2f}")
    conn.close()
    print()

    # サーバー起動
    server, port = start_server(args.port)
    url = f"http://localhost:{port}"

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    print(f"ダッシュボード: {url}")
    print("終了するには Ctrl+C を押してください")
    print()

    # シグナルハンドラ
    def shutdown(sig, frame):
        print("\nサーバーを停止中...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
