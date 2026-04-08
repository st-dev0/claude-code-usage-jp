"""
server.py — HTTPサーバー + APIエンドポイント
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from scanner import DB_PATH, init_db, scan, PRICING

DASHBOARD_HTML = None  # 起動時にロード
TIPS_DATA = None


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def get_conn():
    """リクエストごとにDB接続を生成"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def load_dashboard():
    """dashboard.htmlをメモリにロード"""
    global DASHBOARD_HTML
    html_path = Path(__file__).parent / "dashboard.html"
    with open(html_path, "r", encoding="utf-8") as f:
        DASHBOARD_HTML = f.read()


def load_tips():
    """tips.jsonをロード"""
    global TIPS_DATA
    tips_path = Path(__file__).parent / "tips.json"
    if tips_path.exists():
        with open(tips_path, "r", encoding="utf-8") as f:
            TIPS_DATA = json.load(f)
    else:
        TIPS_DATA = []


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # アクセスログ抑制

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_params(self):
        parsed = urlparse(self.path)
        return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def do_GET(self):
        path, params = self._parse_params()

        if path == "/":
            self._send_html(DASHBOARD_HTML)
        elif path == "/api/daily":
            self._handle_daily(params)
        elif path == "/api/projects":
            self._handle_projects(params)
        elif path == "/api/sessions":
            self._handle_sessions(params)
        elif path == "/api/summary":
            self._handle_summary(params)
        elif path == "/api/config":
            self._handle_config_get()
        elif path == "/api/hourly":
            self._handle_hourly(params)
        elif path == "/api/tips":
            self._send_json(TIPS_DATA or [])
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path, _ = self._parse_params()

        if path == "/api/config":
            self._handle_config_post()
        elif path == "/api/rescan":
            self._handle_rescan()
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ─── API ハンドラ ───

    def _handle_daily(self, params):
        """日別トークン/コスト（モデル別）"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_from = params.get("from", (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"))
        date_to = params.get("to", today)

        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT date(timestamp) as day, model,
                       SUM(input_tokens) as input_tok,
                       SUM(output_tokens) as output_tok,
                       SUM(cache_creation_input_tokens) as cache_create,
                       SUM(cache_read_input_tokens) as cache_read,
                       SUM(cost_estimate) as cost
                FROM turns
                WHERE date(timestamp) >= ? AND date(timestamp) <= ?
                GROUP BY day, model
                ORDER BY day
            """, (date_from, date_to)).fetchall()

            # 日付一覧とモデル一覧を構築
            dates_set = set()
            models_set = set()
            data_map = {}
            for r in rows:
                day = r["day"]
                model = r["model"]
                dates_set.add(day)
                models_set.add(model)
                data_map[(day, model)] = {
                    "input_tokens": r["input_tok"],
                    "output_tokens": r["output_tok"],
                    "cache_creation": r["cache_create"],
                    "cache_read": r["cache_read"],
                    "cost": round(r["cost"], 4),
                }

            dates = sorted(dates_set)
            models = sorted(models_set)

            result = {"dates": dates, "models": {}}
            for model in models:
                result["models"][model] = {
                    "input_tokens": [],
                    "output_tokens": [],
                    "cache_creation": [],
                    "cache_read": [],
                    "cost": [],
                }
                for day in dates:
                    d = data_map.get((day, model), {})
                    result["models"][model]["input_tokens"].append(d.get("input_tokens", 0))
                    result["models"][model]["output_tokens"].append(d.get("output_tokens", 0))
                    result["models"][model]["cache_creation"].append(d.get("cache_creation", 0))
                    result["models"][model]["cache_read"].append(d.get("cache_read", 0))
                    result["models"][model]["cost"].append(d.get("cost", 0))

            # 日ごとの合計コスト
            total_by_date = []
            for day in dates:
                total = sum(data_map.get((day, m), {}).get("cost", 0) for m in models)
                total_by_date.append(round(total, 4))
            result["total_cost_by_date"] = total_by_date

            self._send_json(result)
        finally:
            conn.close()

    def _handle_projects(self, params):
        """プロジェクト別集計"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_from = params.get("from", "2000-01-01")
        date_to = params.get("to", today)
        limit = int(params.get("limit", "20"))

        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT project_path,
                       SUM(cost_estimate) as total_cost,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(cache_creation_input_tokens) as total_cache_creation,
                       SUM(cache_read_input_tokens) as total_cache_read,
                       COUNT(DISTINCT session_id) as session_count,
                       COUNT(*) as turn_count
                FROM turns
                WHERE date(timestamp) >= ? AND date(timestamp) <= ?
                GROUP BY project_path
                ORDER BY total_cost DESC
                LIMIT ?
            """, (date_from, date_to, limit)).fetchall()

            projects = []
            for r in rows:
                path = r["project_path"] or ""
                parts = Path(path).parts if path else []
                display = parts[-1] if parts else "不明"
                # モデル別内訳
                model_rows = conn.execute("""
                    SELECT model, SUM(output_tokens) as output_tokens
                    FROM turns
                    WHERE project_path = ? AND date(timestamp) >= ? AND date(timestamp) <= ?
                    GROUP BY model
                """, (path, date_from, date_to)).fetchall()
                by_model = {mr["model"]: mr["output_tokens"] for mr in model_rows}

                projects.append({
                    "project_path": path,
                    "display_name": display,
                    "total_cost": round(r["total_cost"], 2),
                    "total_input": r["total_input"],
                    "total_output": r["total_output"],
                    "total_cache_creation": r["total_cache_creation"],
                    "total_cache_read": r["total_cache_read"],
                    "session_count": r["session_count"],
                    "turn_count": r["turn_count"],
                    "by_model": by_model,
                })

            self._send_json({"projects": projects})
        finally:
            conn.close()

    def _handle_sessions(self, params):
        """セッション一覧 + プロンプト"""
        date = params.get("date")
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT session_id, project_path, project_display,
                       start_time, end_time, total_cost, turn_count,
                       prompt_count, primary_model,
                       total_input_tokens, total_output_tokens
                FROM sessions
                WHERE date(start_time) = ?
                ORDER BY start_time DESC
            """, (date,)).fetchall()

            sessions = []
            for r in rows:
                # プロンプト取得
                prompt_rows = conn.execute(
                    "SELECT timestamp, content FROM prompts WHERE session_id = ? ORDER BY timestamp",
                    (r["session_id"],),
                ).fetchall()

                prompts = [{"timestamp": p["timestamp"], "content": p["content"]} for p in prompt_rows]

                # 所要時間計算
                duration_min = None
                if r["start_time"] and r["end_time"]:
                    try:
                        start = datetime.fromisoformat(r["start_time"].replace("Z", "+00:00"))
                        end = datetime.fromisoformat(r["end_time"].replace("Z", "+00:00"))
                        duration_min = max(1, int((end - start).total_seconds() / 60))
                    except (ValueError, TypeError):
                        pass

                sessions.append({
                    "session_id": r["session_id"],
                    "project_display": r["project_display"] or "不明",
                    "project_path": r["project_path"] or "",
                    "start_time": r["start_time"],
                    "end_time": r["end_time"],
                    "duration_minutes": duration_min,
                    "total_cost": round(r["total_cost"], 2),
                    "turn_count": r["turn_count"],
                    "prompt_count": r["prompt_count"],
                    "primary_model": r["primary_model"],
                    "total_input": r["total_input_tokens"],
                    "total_output": r["total_output_tokens"],
                    "prompts": prompts,
                })

            self._send_json({"date": date, "sessions": sessions})
        finally:
            conn.close()

    def _handle_summary(self, params):
        """サマリー（前期比較付き）"""
        period = params.get("period", "month")
        now = datetime.now(timezone.utc)

        if period == "week":
            current_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            prev_start = (now - timedelta(days=now.weekday() + 7)).strftime("%Y-%m-%d")
            prev_end = (now - timedelta(days=now.weekday() + 1)).strftime("%Y-%m-%d")
            current_label = "今週"
            prev_label = "先週"
        else:
            current_start = now.strftime("%Y-%m-01")
            prev_month = now.replace(day=1) - timedelta(days=1)
            prev_start = prev_month.strftime("%Y-%m-01")
            prev_end = prev_month.strftime("%Y-%m-%d")
            current_label = now.strftime("%Y年%m月")
            prev_label = prev_month.strftime("%Y年%m月")

        current_end = now.strftime("%Y-%m-%d")

        conn = get_conn()
        try:
            def get_period_data(start, end):
                row = conn.execute("""
                    SELECT COALESCE(SUM(cost_estimate), 0) as total_cost,
                           COALESCE(SUM(input_tokens), 0) as total_input,
                           COALESCE(SUM(output_tokens), 0) as total_output,
                           COALESCE(SUM(cache_creation_input_tokens), 0) as total_cache_creation,
                           COALESCE(SUM(cache_read_input_tokens), 0) as total_cache_read,
                           COUNT(DISTINCT session_id) as session_count,
                           COUNT(*) as turn_count
                    FROM turns
                    WHERE date(timestamp) >= ? AND date(timestamp) <= ?
                """, (start, end)).fetchone()

                # モデル別内訳
                model_rows = conn.execute("""
                    SELECT model,
                           SUM(cost_estimate) as cost,
                           COUNT(*) as turns
                    FROM turns
                    WHERE date(timestamp) >= ? AND date(timestamp) <= ?
                    GROUP BY model
                """, (start, end)).fetchall()

                by_model = {}
                for mr in model_rows:
                    by_model[mr["model"]] = {
                        "cost": round(mr["cost"], 2),
                        "turns": mr["turns"],
                    }

                # サブエージェント集計
                sub_row = conn.execute("""
                    SELECT COALESCE(SUM(CASE WHEN is_subagent = 1 THEN 1 ELSE 0 END), 0) as sub,
                           COALESCE(SUM(CASE WHEN is_subagent = 0 THEN 1 ELSE 0 END), 0) as main
                    FROM turns
                    WHERE date(timestamp) >= ? AND date(timestamp) <= ?
                """, (start, end)).fetchone()

                return {
                    "total_cost": round(row["total_cost"], 2),
                    "total_input": row["total_input"],
                    "total_output": row["total_output"],
                    "total_cache_creation": row["total_cache_creation"],
                    "total_cache_read": row["total_cache_read"],
                    "session_count": row["session_count"],
                    "turn_count": row["turn_count"],
                    "subagent_turns": sub_row["sub"],
                    "main_turns": sub_row["main"],
                    "by_model": by_model,
                }

            current_data = get_period_data(current_start, current_end)
            current_data["label"] = current_label
            prev_data = get_period_data(prev_start, prev_end)
            prev_data["label"] = prev_label

            # プラン情報
            config_row = conn.execute(
                "SELECT value FROM config WHERE key = 'plan'"
            ).fetchone()
            plan = config_row["value"] if config_row else "max_5x"

            plan_prices = {"pro": 20, "max_5x": 100, "max_20x": 200}
            monthly_price = plan_prices.get(plan, 100)

            current_data["plan_info"] = {
                "plan": plan,
                "monthly_price": monthly_price,
                "equivalent_api_cost": current_data["total_cost"],
                "savings_ratio": round(current_data["total_cost"] / monthly_price, 1) if monthly_price > 0 else 0,
            }

            # 前期比
            change_pct = 0
            if prev_data["total_cost"] > 0:
                change_pct = round(
                    (current_data["total_cost"] - prev_data["total_cost"]) / prev_data["total_cost"] * 100, 1
                )

            # 全期間集計
            all_row = conn.execute("""
                SELECT COALESCE(SUM(total_cost), 0) as cost,
                       COUNT(*) as sessions,
                       COALESCE(SUM(turn_count), 0) as turns
                FROM sessions
            """).fetchone()

            self._send_json({
                "current": current_data,
                "previous": prev_data,
                "change_pct": change_pct,
                "all_time": {
                    "total_cost": round(all_row["cost"], 2),
                    "total_sessions": all_row["sessions"],
                    "total_turns": all_row["turns"],
                },
            })
        finally:
            conn.close()

    def _handle_hourly(self, params):
        """時間帯別アクティビティ"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_from = params.get("from", (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"))
        date_to = params.get("to", today)

        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT CAST(strftime('%H', timestamp, '+9 hours') AS INTEGER) as hour,
                       COUNT(*) as turn_count,
                       SUM(output_tokens) as output_tokens
                FROM turns
                WHERE date(timestamp) >= ? AND date(timestamp) <= ?
                GROUP BY hour
                ORDER BY hour
            """, (date_from, date_to)).fetchall()

            # 0〜23時のデータを埋める
            hour_map = {r["hour"]: {"turn_count": r["turn_count"], "output_tokens": r["output_tokens"]} for r in rows}
            hours = []
            for h in range(24):
                d = hour_map.get(h, {"turn_count": 0, "output_tokens": 0})
                hours.append({"hour": h, "turn_count": d["turn_count"], "output_tokens": d["output_tokens"]})

            self._send_json({"hours": hours})
        finally:
            conn.close()

    def _handle_config_get(self):
        conn = get_conn()
        try:
            rows = conn.execute("SELECT key, value FROM config").fetchall()
            config = {r["key"]: r["value"] for r in rows}
            # デフォルト値
            config.setdefault("plan", "max_5x")
            config.setdefault("theme", "dark")
            config.setdefault("mode", "simple")
            self._send_json(config)
        finally:
            conn.close()

    def _handle_config_post(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        conn = get_conn()
        try:
            for key, value in data.items():
                if key in ("plan", "theme", "mode"):
                    conn.execute(
                        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                        (key, str(value)),
                    )
            conn.commit()
            self._send_json({"ok": True})
        finally:
            conn.close()

    def _handle_rescan(self):
        conn = get_conn()
        try:
            start = datetime.now(timezone.utc)
            scanned, new_turns = scan(conn)
            duration = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            self._send_json({
                "scanned": scanned,
                "new_turns": new_turns,
                "duration_ms": duration,
            })
        finally:
            conn.close()


def start_server(port=8080):
    """サーバー起動"""
    load_dashboard()
    load_tips()

    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), DashboardHandler)
            print(f"サーバー起動: http://localhost:{p}")
            return server, p
        except OSError:
            continue

    raise RuntimeError(f"ポート {port}〜{port+9} がすべて使用中です")
