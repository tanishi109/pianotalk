#!/bin/zsh
# 録音デーモンを launchd（ログイン時に自動起動・落ちたら再起動）に登録する。
#   ./launchd/install.sh            登録して起動
#   ./launchd/install.sh uninstall  停止して登録解除
set -euo pipefail

LABEL="com.pianotalk.recorder"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/PianoTalk/logs"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "登録を解除しました（録音ファイルは残っています）。"
  exit 0
fi

BIN="$PROJECT/.venv/bin/pianotalk-recorder"
if [[ ! -x "$BIN" ]]; then
  echo "$BIN がありません。先にプロジェクトで 'uv sync' を実行してください。" >&2
  exit 1
fi
mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$BIN</string></array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>RunAtLoad</key><true/>
  <!-- 異常終了したときだけ再起動。メニューの「終了」や SIGTERM（正常終了）では止まったまま -->
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- Interactive: launchd がバックグラウンド扱いで CPU/タイマーを絞らないようにする -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGDIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# bootout は非同期なので、前のジョブが消えるまで待ってから登録する
for i in {1..20}; do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "登録しました: $PLIST"
echo "ログ: $LOGDIR/recorder.log"
echo "停止: メニューバーの「終了」または launchctl kill TERM $DOMAIN/$LABEL"
echo "起動: launchctl kickstart $DOMAIN/$LABEL"
