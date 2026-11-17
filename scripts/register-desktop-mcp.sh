#!/bin/zsh
# Claude デスクトップアプリの claude_desktop_config.json にある PianoTalk の登録を追加・削除する。
# 通常は .mcpb（デスクトップ拡張機能）でインストールするので、このスクリプトは
# 手動で登録した古い版からの移行（--remove）や、開発中のソースを直接つなぐときに使う。
#
# 起動中のアプリは設定をメモリに持っていて claude_desktop_config.json を書き戻すため、
# アプリが動いている間に外から書き換えても消されてしまう。そこで:
#   1. アプリが終了するまで待つ（最大10分）
#   2. 設定をバックアップして mcpServers.pianotalk を追加／削除（他の項目はそのまま）
#   3. アプリを起動し直す
#
#   ./scripts/register-desktop-mcp.sh [--remove]          このターミナルで待つ（別アプリの Terminal.app から実行）
#   ./scripts/register-desktop-mcp.sh [--remove] --detach launchd の一時ジョブとして待つ（アプリ内から実行してよい）
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$PROJECT/scripts/register-desktop-mcp.sh"
BUNDLE="com.anthropic.claudefordesktop"
CONF="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
LOG="$HOME/PianoTalk/logs/register-desktop-mcp.log"
LABEL="com.pianotalk.register-desktop"
mkdir -p "${LOG:h}"

ACTION=add
DETACH=0
FROM_LAUNCHD=0
for a in "$@"; do
  case "$a" in
    --remove) ACTION=remove ;;
    --detach) DETACH=1 ;;
    --from-launchd) FROM_LAUNCHD=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

if (( DETACH )); then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  EXTRA=""
  [[ $ACTION == remove ]] && EXTRA="<string>--remove</string>"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$SELF</string>$EXTRA<string>--from-launchd</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "待機を開始しました。Claude アプリを ⌘Q で終了してください（10分以内）。ログ: $LOG"
  exit 0
fi

running() { [[ "$(osascript -e "application id \"$BUNDLE\" is running")" == "true" ]] }

echo "$(date '+%F %T') waiting for Claude to quit ($ACTION)…"
for i in {1..600}; do
  running || break
  sleep 1
done
if running; then
  echo "$(date '+%F %T') Claude did not quit within 10 minutes; nothing changed."
else
  sleep 2  # let the app finish writing its files
  cp "$CONF" "$CONF.bak-$(date +%Y%m%d-%H%M%S)"
  /usr/bin/python3 - "$CONF" "$ACTION" "$PROJECT/.venv/bin/pianotalk-mcp" <<'EOF'
import json, os, sys
path, action, cmd = sys.argv[1:4]
with open(path) as f:
    d = json.load(f)
servers = d.setdefault("mcpServers", {})
if action == "add":
    servers["pianotalk"] = {"command": cmd}
else:
    servers.pop("pianotalk", None)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("mcpServers:", sorted(servers))
EOF
  echo "$(date '+%F %T') done ($ACTION); relaunching Claude"
  open -b "$BUNDLE"
fi

if (( FROM_LAUNCHD )); then
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl remove "$LABEL"  # unloads this one-shot job (ends this process)
fi
