#!/bin/bash
# install_mac_scheduler.sh — 競輪データ取得を Mac の launchd で全自動化するインストーラ
#
# 使い方: pinpoint.py / daily_update.py / backfill_midnight.py と同じフォルダに置いて
#         `bash install_mac_scheduler.sh` を1回実行するだけ。
# やること:
#   1. 実行用ランナー run_keirin_job.sh と env.sh（無ければ雛形）をこのフォルダに作る
#   2. ~/Library/LaunchAgents に launchd 設定（plist）を4つ作る
#   3. launchctl で有効化する
# スケジュール（日本時間）:
#   pinpoint.py（PRO予想のみ）      : 07:30 / 09:00 / 12:00 / 15:00 / 18:00 / 21:00 / 23:00
#   daily_update.py（出走表・結果） : 23:50（当日分）
#   daily_update.py（前日分の回収） : 04:30（DAYS_OFFSET=1）
#   backfill_midnight.py（不足分）  : 03:30（対象が無くなれば自動で空振り終了）
# ログ: このフォルダの logs/ 配下。止め方: bash uninstall_mac_scheduler.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

for f in pinpoint.py daily_update.py; do
  if [ ! -f "$f" ]; then
    echo "❌ $f がこのフォルダにありません。pinpoint.py と同じフォルダで実行してください。"
    exit 1
  fi
done

# backfill は任意（backfill_midnight.py と backfill.py の両方が揃った場合だけ有効化）
HAS_BACKFILL=0
if [ -f backfill_midnight.py ] && [ -f backfill.py ]; then
  HAS_BACKFILL=1
else
  echo "ℹ️ backfill_midnight.py / backfill.py が無いため、03:30の補完ジョブは今回スキップします（PRO予想と全データの自動化は有効になります）"
fi

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "❌ python3 が見つかりません。"
  exit 1
fi
echo "$PY" > .keirin_python
echo "🐍 Python: $PY"
echo "📁 対象フォルダ: $DIR"

# --- env.sh（DATABASE_URL 用） ---
if [ ! -f env.sh ]; then
  if [ -n "${DATABASE_URL:-}" ]; then
    printf 'export DATABASE_URL=%q\n' "$DATABASE_URL" > env.sh
    echo "🔑 env.sh を作成しました（現在のシェルの DATABASE_URL を保存）"
  else
    cat > env.sh <<'EOF'
# ここに Supabase 接続URLを1行で書いてください（手動実行で使っているものと同じ）
export DATABASE_URL=
EOF
    echo "⚠️ env.sh の雛形を作りました。DATABASE_URL を書き込んでから、もう一度このスクリプトを実行してください。"
    echo "   書き方: export DATABASE_URL=postgres://...（URL全体をそのまま）"
    exit 1
  fi
else
  echo "🔑 env.sh: 既存のものを使います"
fi

# --- ランナー ---
cat > run_keirin_job.sh <<'RUNNER'
#!/bin/bash
# run_keirin_job.sh <pinpoint|daily|daily_yesterday|backfill>
JOB="${1:-pinpoint}"
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
mkdir -p logs
LOG="logs/${JOB}.log"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') start: $JOB ====="
  if [ -f env.sh ]; then set -a; . ./env.sh; set +a; fi
  PY="$(cat .keirin_python 2>/dev/null || command -v python3 || echo /usr/bin/python3)"
  case "$JOB" in
    pinpoint)        "$PY" pinpoint.py ;;
    daily)           "$PY" daily_update.py ;;
    daily_yesterday) DAYS_OFFSET=1 "$PY" daily_update.py ;;
    backfill)        "$PY" backfill_midnight.py ;;
    *) echo "unknown job: $JOB" ;;
  esac
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') end: $JOB ====="
} >> "$LOG" 2>&1
RUNNER
chmod +x run_keirin_job.sh

# --- アンインストーラ ---
cat > uninstall_mac_scheduler.sh <<'UNINST'
#!/bin/bash
for label in com.keirin.pinpoint com.keirin.daily com.keirin.daily-sweep com.keirin.backfill; do
  launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "🛑 スケジュールを停止しました（plistを削除）。スクリプトとログはフォルダに残っています。"
UNINST
chmod +x uninstall_mac_scheduler.sh

# --- plist 生成 ---
AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"
RUNNER_PATH="$DIR/run_keirin_job.sh"

make_plist() {
  local label="$1" job="$2" times_xml="$3"
  cat > "$AGENTS_DIR/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$RUNNER_PATH</string>
    <string>$job</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$DIR</string>
  <key>StartCalendarInterval</key>
$times_xml
  <key>StandardOutPath</key>
  <string>$DIR/logs/launchd-$job.out</string>
  <key>StandardErrorPath</key>
  <string>$DIR/logs/launchd-$job.err</string>
</dict>
</plist>
EOF
}

time_one() { echo "  <dict><key>Hour</key><integer>$1</integer><key>Minute</key><integer>$2</integer></dict>"; }

PIN_TIMES="  <array>
$(time_one 7 30)
$(time_one 9 0)
$(time_one 12 0)
$(time_one 15 0)
$(time_one 18 0)
$(time_one 21 0)
$(time_one 23 0)
  </array>"
ONE_2350="  <dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>50</integer></dict>"
ONE_0430="  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>"
ONE_0330="  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>"

mkdir -p logs
make_plist com.keirin.pinpoint pinpoint "$PIN_TIMES"
make_plist com.keirin.daily daily "$ONE_2350"
make_plist com.keirin.daily-sweep daily_yesterday "$ONE_0430"
LABELS="com.keirin.pinpoint com.keirin.daily com.keirin.daily-sweep"
if [ "$HAS_BACKFILL" = "1" ]; then
  make_plist com.keirin.backfill backfill "$ONE_0330"
  LABELS="$LABELS com.keirin.backfill"
fi

# --- 有効化 ---
for label in $LABELS; do
  if command -v launchctl >/dev/null 2>&1; then
    launchctl unload "$AGENTS_DIR/$label.plist" 2>/dev/null || true
    launchctl load "$AGENTS_DIR/$label.plist"
    echo "✅ 有効化: $label"
  else
    echo "📝 生成のみ（launchctl が無い環境）: $label"
  fi
done

cat <<'DONE'

🎉 セットアップ完了。スケジュール起動で全自動になりました。

  PRO予想 pinpoint : 07:30 / 09:00 / 12:00 / 15:00 / 18:00 / 21:00 / 23:00
  全データ daily   : 23:50（当日分）＋ 04:30（前日分の取りこぼし回収）
  補完 backfill    : 03:30（backfill一式がある場合のみ。ミッドナイト不足分の消化用）

  ログ確認: このフォルダの logs/ を見る
  停止方法: bash uninstall_mac_scheduler.sh
  注意: Macが起動してログインしている必要があります。
        スリープしていた回は、起きた直後にまとめて実行されます。
DONE
