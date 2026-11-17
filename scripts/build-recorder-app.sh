#!/bin/zsh
# PianoTalk Recorder.app をこの Mac の上でビルドし、確認してからアプリケーションフォルダに置く。
#
#   ./scripts/build-recorder-app.sh          ビルドして、置くかどうか確認する
#   ./scripts/build-recorder-app.sh --yes    確認せずに置く
#   ./scripts/build-recorder-app.sh --build-only  dist/app にビルドするだけ
#
# 手元でビルドしたアプリは quarantine 属性が付かないので、notarize なしで起動できる。
# 署名は ad-hoc（この Mac の上でだけ有効な署名）。
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="PianoTalk Recorder.app"
BUNDLE_ID="io.github.tanishi109.pianotalk.recorder"
BUILT="$PROJECT/dist/app/$APP_NAME"

YES=0
BUILD_ONLY=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --build-only) BUILD_ONLY=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

UV="$(command -v uv || true)"
[[ -z "$UV" && -x "$HOME/.local/bin/uv" ]] && UV="$HOME/.local/bin/uv"
if [[ -z "$UV" ]]; then
  echo "uv が見つかりません。README の「1. Python 環境」の手順で入れてください。" >&2
  exit 1
fi

echo "ビルドしています（1〜2分かかります）…"
cd "$PROJECT"
"$UV" sync -q --group app
rm -rf "$PROJECT/app/build" "$PROJECT/dist/app"
mkdir -p "$PROJECT/dist"
(cd "$PROJECT/app" && "$PROJECT/.venv/bin/python" setup.py py2app --dist-dir "$PROJECT/dist/app" \
  > "$PROJECT/dist/py2app.log" 2>&1) || { echo "ビルドに失敗しました。ログ: dist/py2app.log" >&2; exit 1; }
rm -rf "$PROJECT/app/build"
codesign --force --deep --sign - "$BUILT"
codesign --verify --deep "$BUILT"
echo "ビルドしました: $BUILT"
(( BUILD_ONLY )) && exit 0

if [[ -w /Applications ]]; then DEST_DIR=/Applications; else DEST_DIR="$HOME/Applications"; fi
DEST="$DEST_DIR/$APP_NAME"

if (( ! YES )); then
  print -n "「$DEST」に置きますか？ [y/N] "
  read -r answer
  [[ "$answer" == [yY]* ]] || { echo "置きませんでした。ビルドしたアプリは $BUILT にあります。"; exit 0; }
fi

if [[ -e "$DEST" ]]; then
  existing="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$DEST/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "$existing" != "$BUNDLE_ID" ]]; then
    echo "「$DEST」は PianoTalk Recorder ではないため、上書きしませんでした。" >&2
    exit 1
  fi
  if pgrep -f "$DEST/Contents/MacOS/" >/dev/null; then
    echo "PianoTalk Recorder が起動中です。メニューバーのアイコン →「終了」で止めてから、もう一度実行してください。" >&2
    exit 1
  fi
  rm -rf "$DEST"
fi
mkdir -p "$DEST_DIR"
ditto "$BUILT" "$DEST"
echo "置きました: $DEST"
echo "起動するには: open \"$DEST\"（Launchpad や Spotlight からも開けます）"
