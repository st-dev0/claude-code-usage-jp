"""
scanner.py — Claude Code JSONLログをパースしてSQLiteに書き込む
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─── 価格テーブル（2026年4月時点、$/MTok） ───
PRICING = {
    "claude-opus-4-6": {
        "input": 6.15, "output": 30.75,
        "cache_write": 7.69, "cache_read": 0.61,
    },
    "claude-sonnet-4-6": {
        "input": 3.69, "output": 18.45,
        "cache_write": 4.61, "cache_read": 0.37,
    },
    "claude-haiku-4-5": {
        "input": 1.23, "output": 6.15,
        "cache_write": 1.54, "cache_read": 0.12,
    },
}

DB_PATH = Path.home() / ".claude" / "usage-jp.db"
CLAUDE_DIR = Path.home() / ".claude" / "projects"

# ─── DB初期化 ───

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    cost_estimate REAL DEFAULT 0.0,
    project_path TEXT,
    is_subagent INTEGER DEFAULT 0,
    UNIQUE(session_id, request_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_path TEXT,
    project_display TEXT,
    start_time TEXT,
    end_time TEXT,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cache_creation INTEGER DEFAULT 0,
    total_cache_read INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    turn_count INTEGER DEFAULT 0,
    prompt_count INTEGER DEFAULT 0,
    primary_model TEXT
);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_state (
    file_path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    file_size INTEGER NOT NULL,
    scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
CREATE INDEX IF NOT EXISTS idx_turns_project ON turns(project_path);
CREATE INDEX IF NOT EXISTS idx_turns_model ON turns(model);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_path);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);
CREATE INDEX IF NOT EXISTS idx_prompts_timestamp ON prompts(timestamp);
"""


def init_db(db_path=None):
    """DBを初期化してコネクションを返す"""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# ─── ユーティリティ ───

def normalize_model(model_name):
    """モデル名を正規化（日付サフィックス除去、ファミリ統一）"""
    if not model_name or model_name == "<synthetic>":
        return None
    m = model_name.lower()
    if "opus" in m:
        return "claude-opus-4-6"
    if "sonnet" in m:
        return "claude-sonnet-4-6"
    if "haiku" in m:
        return "claude-haiku-4-5"
    return model_name


def calc_cost(model, input_tok, output_tok, cache_creation, cache_read):
    """API換算コストを計算（USD）"""
    p = PRICING.get(model, PRICING["claude-sonnet-4-6"])
    cost = (
        input_tok * p["input"] / 1_000_000
        + output_tok * p["output"] / 1_000_000
        + cache_creation * p["cache_write"] / 1_000_000
        + cache_read * p["cache_read"] / 1_000_000
    )
    return round(cost, 6)


def extract_session_id(fpath):
    """ファイルパスからセッションIDを抽出"""
    p = Path(fpath)
    # subagents の場合: .../{session-id}/subagents/{agent}.jsonl
    if "subagents" in p.parts:
        idx = p.parts.index("subagents")
        return p.parts[idx - 1]
    # メインセッション: .../{session-id}.jsonl
    return p.stem


def extract_prompt_text(message):
    """userメッセージからプロンプトテキストを抽出"""
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return str(content)


def project_display_name(project_path):
    """プロジェクトパスから表示名を生成"""
    if not project_path:
        return "不明"
    parts = Path(project_path).parts
    # 最後の非空パーツ
    for part in reversed(parts):
        if part and part != "/":
            return part
    return project_path


# ─── JSONLパース ───

def parse_jsonl_file(fpath, session_id, is_subagent):
    """1つのJSONLファイルをパースしてターン・プロンプトを返す"""
    project_path = None
    request_usage = {}  # requestId -> usage data (最後のエントリを採用)
    prompts = []

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # プロジェクトパス抽出（最初に見つかったcwd）
                if not project_path and obj.get("cwd"):
                    project_path = obj["cwd"]

                msg_type = obj.get("type")

                # ユーザープロンプト抽出（メインセッションのみ）
                if (not is_subagent
                        and msg_type == "user"
                        and obj.get("userType") == "external"
                        and "toolUseResult" not in obj
                        and not obj.get("sourceToolUseID")
                        and not obj.get("isMeta")):
                    msg = obj.get("message", {})
                    text = extract_prompt_text(msg)
                    if text and len(text.strip()) > 0:
                        prompts.append({
                            "session_id": session_id,
                            "timestamp": obj.get("timestamp", ""),
                            "content": text[:500],
                        })

                # アシスタントメッセージのusage抽出
                if msg_type == "assistant":
                    msg = obj.get("message", {})
                    model_raw = msg.get("model", "")
                    model = normalize_model(model_raw)
                    if not model:
                        continue
                    usage = msg.get("usage")
                    if not usage:
                        continue

                    req_id = obj.get("requestId") or obj.get("uuid", "")
                    if not req_id:
                        continue

                    # 同じrequestIdの最後のエントリを採用（ストリーミング対応）
                    request_usage[req_id] = {
                        "timestamp": obj.get("timestamp", ""),
                        "model": model,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_creation": usage.get("cache_creation_input_tokens", 0),
                        "cache_read": usage.get("cache_read_input_tokens", 0),
                    }
    except (OSError, UnicodeDecodeError):
        return [], [], project_path

    # ターンデータ構築
    turns = []
    for req_id, data in request_usage.items():
        cost = calc_cost(
            data["model"],
            data["input_tokens"],
            data["output_tokens"],
            data["cache_creation"],
            data["cache_read"],
        )
        turns.append({
            "session_id": session_id,
            "request_id": req_id,
            "timestamp": data["timestamp"],
            "model": data["model"],
            "input_tokens": data["input_tokens"],
            "output_tokens": data["output_tokens"],
            "cache_creation": data["cache_creation"],
            "cache_read": data["cache_read"],
            "cost_estimate": cost,
            "project_path": project_path,
            "is_subagent": 1 if is_subagent else 0,
        })

    return turns, prompts, project_path


# ─── スキャン ───

def find_all_jsonl():
    """~/.claude/projects/ 以下の全JSONLファイルを返す"""
    files = []
    if not CLAUDE_DIR.exists():
        return files
    for dirpath, _dirs, filenames in os.walk(CLAUDE_DIR):
        for fname in filenames:
            if fname.endswith(".jsonl"):
                files.append(os.path.join(dirpath, fname))
    return files


def scan(conn, force=False):
    """インクリメンタルスキャン実行。戻り値: (scanned_files, new_turns)"""
    all_files = find_all_jsonl()
    to_scan = []

    for fpath in all_files:
        try:
            stat = os.stat(fpath)
        except OSError:
            continue
        current_mtime = stat.st_mtime
        current_size = stat.st_size

        if not force:
            row = conn.execute(
                "SELECT mtime, file_size FROM scan_state WHERE file_path = ?",
                (fpath,),
            ).fetchone()
            if row and row["mtime"] == current_mtime and row["file_size"] == current_size:
                continue

        to_scan.append((fpath, current_mtime, current_size))

    if not to_scan:
        return 0, 0

    total_turns = 0

    conn.execute("BEGIN")
    try:
        for fpath, mtime, size in to_scan:
            session_id = extract_session_id(fpath)
            is_subagent = "subagents" in fpath

            # 既存データ削除（再スキャン対応）
            if is_subagent:
                agent_stem = Path(fpath).stem
                conn.execute(
                    "DELETE FROM turns WHERE session_id = ? AND request_id LIKE ? AND is_subagent = 1",
                    (session_id, f"%{agent_stem}%"),
                )
            else:
                conn.execute(
                    "DELETE FROM turns WHERE session_id = ? AND is_subagent = 0",
                    (session_id,),
                )
                conn.execute(
                    "DELETE FROM prompts WHERE session_id = ?",
                    (session_id,),
                )

            # パース & 挿入
            turns, prompts, _project = parse_jsonl_file(fpath, session_id, is_subagent)

            for t in turns:
                conn.execute(
                    """INSERT OR REPLACE INTO turns
                       (session_id, request_id, timestamp, model,
                        input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens,
                        cost_estimate, project_path, is_subagent)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (t["session_id"], t["request_id"], t["timestamp"],
                     t["model"], t["input_tokens"], t["output_tokens"],
                     t["cache_creation"], t["cache_read"],
                     t["cost_estimate"], t["project_path"], t["is_subagent"]),
                )
            total_turns += len(turns)

            for p in prompts:
                conn.execute(
                    "INSERT INTO prompts (session_id, timestamp, content) VALUES (?, ?, ?)",
                    (p["session_id"], p["timestamp"], p["content"]),
                )

            # scan_state更新
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO scan_state (file_path, mtime, file_size, scanned_at) VALUES (?, ?, ?, ?)",
                (fpath, mtime, size, now),
            )

        # sessions再構築
        rebuild_sessions(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return len(to_scan), total_turns


def rebuild_sessions(conn):
    """turnsテーブルからsessionsを再構築"""
    conn.execute("DELETE FROM sessions")
    conn.execute("""
        INSERT INTO sessions (
            session_id, project_path, project_display,
            start_time, end_time,
            total_input_tokens, total_output_tokens,
            total_cache_creation, total_cache_read,
            total_cost, turn_count, prompt_count, primary_model
        )
        SELECT
            t.session_id,
            t.project_path,
            t.project_path,
            MIN(t.timestamp),
            MAX(t.timestamp),
            SUM(t.input_tokens),
            SUM(t.output_tokens),
            SUM(t.cache_creation_input_tokens),
            SUM(t.cache_read_input_tokens),
            SUM(t.cost_estimate),
            COUNT(*),
            COALESCE((SELECT COUNT(*) FROM prompts p WHERE p.session_id = t.session_id), 0),
            (SELECT t2.model FROM turns t2
             WHERE t2.session_id = t.session_id
             GROUP BY t2.model ORDER BY COUNT(*) DESC LIMIT 1)
        FROM turns t
        GROUP BY t.session_id
    """)

    # project_displayをPythonで整形
    rows = conn.execute("SELECT session_id, project_path FROM sessions").fetchall()
    for row in rows:
        display = project_display_name(row["project_path"])
        conn.execute(
            "UPDATE sessions SET project_display = ? WHERE session_id = ?",
            (display, row["session_id"]),
        )


# ─── CLI直接実行 ───

if __name__ == "__main__":
    print("Claude Code 使用量スキャナー")
    print(f"データベース: {DB_PATH}")
    print(f"スキャン対象: {CLAUDE_DIR}")
    print()

    conn = init_db()
    force = "--force" in sys.argv

    print("スキャン中...")
    scanned, turns = scan(conn, force=force)
    print(f"完了: {scanned} ファイル処理、{turns} ターン追加")

    # サマリー表示
    row = conn.execute(
        "SELECT COUNT(*) as sessions, SUM(turn_count) as turns, SUM(total_cost) as cost FROM sessions"
    ).fetchone()
    print(f"\n累計: {row['sessions']} セッション, {row['turns']} ターン, ${row['cost']:.2f} API換算コスト")
    conn.close()
