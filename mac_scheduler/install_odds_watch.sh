#!/bin/bash
# install_odds_watch.sh — EV監視（odds_watch.py）を Mac の launchd に追加する
#
# 使い方: odds_watch.py / ev_watch.py と同じフォルダ（例: ~/keirin_v3）に
#         このファイルを置いて `bash install_odds_watch.sh` を1回実行するだけ。
# 既存の4ジョブ（pinpoint等）には一切触らない。
# やること:
#   1. 実行用ランナー run_odds_watch.sh をこのフォルダに作る
#   2. ~/Library/LaunchAgents に plist（com.keirin.oddswatch）を作る
#   3. launchctl で有効化する（10分おきに実行。対象が無い時はすぐ終了する）
# ログ: このフォルダの logs/oddswatch.log
# 止め方: bash uninstall_odds_watch.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

for f in odds_watch.py ev_watch.py; do
  if [ ! -f "$f" ]; then
    echo "❌ $f がこのフォルダにありません。odds_watch.py と同じフォルダで実行してください。"
    exit 1
  fi
done

if [ ! -f env.sh ]; then
  echo "⚠️ env.sh がありません。先に install_mac_scheduler.sh 側の env.sh を用意してください。"
  exit 1
fi

PY="$(cat .keirin_python 2>/dev/null || command -v python3 || echo /usr/bin/python3)"
echo "🐍 Python: $PY"
echo "📁 対象フォルダ: $DIR"

cat > run_odds_watch.sh <<'RUNNER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
mkdir -p logs
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') start: oddswatch ====="
  if [ -f env.sh ]; then set -a; . ./env.sh; set +a; fi
  PY="$(cat .keirin_python 2>/dev/null || command -v python3 || echo /usr/bin/python3)"
  "$PY" odds_watch.py
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') end: oddswatch ====="
} >> logs/oddswatch.log 2>&1
RUNNER
chmod +x run_odds_watch.sh

cat > uninstall_odds_watch.sh <<'UNINST'
#!/bin/bash
launchctl unload "$HOME/Library/LaunchAgents/com.keirin.oddswatch.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.keirin.oddswatch.plist"
echo "🛑 EV監視のスケジュールを停止しました（plistを削除）。スクリプトとログはフォルダに残っています。"
UNINST
chmod +x uninstall_odds_watch.sh

AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"
mkdir -p logs

cat > "$AGENTS_DIR/com.keirin.oddswatch.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.keirin.oddswatch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/run_odds_watch.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$DIR</string>
  <key>StartInterval</key>
  <integer>600</integer>
  <key>StandardOutPath</key>
  <string>$DIR/logs/launchd-oddswatch.out</string>
  <key>StandardErrorPath</key>
  <string>$DIR/logs/launchd-oddswatch.err</string>
</dict>
</plist>
EOF

if command -v launchctl >/dev/null 2>&1; then
  launchctl unload "$AGENTS_DIR/com.keirin.oddswatch.plist" 2>/dev/null || true
  launchctl load "$AGENTS_DIR/com.keirin.oddswatch.plist"
  echo "✅ 有効化: com.keirin.oddswatch（10分おき）"
else
  echo "📝 生成のみ（launchctl が無い環境）: com.keirin.oddswatch"
fi

cat <<'DONE'

🎉 EV監視を追加しました。

  EV監視 oddswatch : 10分おき（発走前の買い目だけ実オッズを確認し、判定をDBに残します）
  ログ確認: このフォルダの logs/oddswatch.log
  停止方法: bash uninstall_odds_watch.sh
  注意: Macが起動してログインしている必要があります。
DONE
