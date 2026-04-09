#!/bin/bash
# Claude Code Usage Dashboard — インストーラー
# 使い方: ./install.sh

set -e

TOOL_DIR="$HOME/.claude/tools/usage-dashboard"
CMD_DIR="$HOME/.claude/commands"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=================================="
echo "  Claude Code Usage Dashboard"
echo "  インストーラー"
echo "=================================="
echo ""

# ツールディレクトリにコピー
echo "1. ファイルをインストール中..."
mkdir -p "$TOOL_DIR"
cp "$SCRIPT_DIR/scanner.py" "$TOOL_DIR/"
cp "$SCRIPT_DIR/server.py" "$TOOL_DIR/"
cp "$SCRIPT_DIR/start.py" "$TOOL_DIR/"
cp "$SCRIPT_DIR/dashboard.html" "$TOOL_DIR/"
echo "   → $TOOL_DIR"

# スラッシュコマンド作成
echo "2. /usage コマンドを登録中..."
mkdir -p "$CMD_DIR"
cat > "$CMD_DIR/usage.md" << 'COMMAND'
Claude Code使用量ダッシュボードを起動してください。

以下のコマンドをBashで実行してください:

```
python3 ~/.claude/tools/usage-dashboard/start.py
```

サーバーが起動したら「ダッシュボードを開きました」と報告してください。
ユーザーが終了を指示するまでサーバーは起動したままにしてください。
COMMAND
echo "   → $CMD_DIR/usage.md"

echo ""
echo "インストール完了!"
echo ""
echo "使い方: Claude Codeで /usage と入力するだけ"
echo ""
