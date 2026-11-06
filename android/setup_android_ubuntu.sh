#!/bin/bash
# setup_android_ubuntu.sh — Android（Termux + proot Ubuntu）用の初回セットアップ
# Ubuntu の「中」で1回だけ実行する。pinpoint.py などと同じ ~/keirin フォルダに置くこと。
# 手動実行専用（スケジュール登録はしない）。
set -e
export DEBIAN_FRONTEND=noninteractive

KEIRIN_DIR="/data/data/com.termux/files/home/keirin"
if [ ! -f "$KEIRIN_DIR/pinpoint.py" ]; then
  echo "❌ $KEIRIN_DIR に pinpoint.py がありません。先にファイルを ~/keirin へコピーしてください。"
  exit 1
fi
cd "$KEIRIN_DIR"

echo "=== 1/4 apt パッケージを入れます（数分かかります）==="
apt-get update
apt-get install -y python3 python3-venv python3-pip curl ca-certificates nano

echo "=== 2/4 Python 仮想環境とライブラリを入れます ==="
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install --upgrade pip
pip install playwright psycopg2-binary

echo "=== 3/4 ブラウザ（Chromium）と必要部品を入れます（数分〜十数分）==="
python -m playwright install --with-deps chromium || python -m playwright install chromium

echo "=== 4/4 実行用ファイルを用意します ==="
if [ ! -f env.sh ]; then
  cat > env.sh <<'EOF'
# ここに Supabase 接続URLを1行で書く（Macで使っているものと同じ）
export DATABASE_URL=
EOF
  echo "⚠️ env.sh の雛形を作りました。あとで DATABASE_URL を書き込んでください（手順は README）"
fi

# Termux側から1コマンドで実行するためのランナー（手動実行用）
make_runner() {
  local name="$1" target="$2" extra_env="$3"
  cat > "$name" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Termux側で実行する: bash ~/keirin/$name
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true
proot-distro login ubuntu -- bash -c 'cd $KEIRIN_DIR && set -a && . ./env.sh && set +a && $extra_env .venv/bin/python $target'
command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock || true
EOF
  chmod +x "$name"
}
make_runner run_pinpoint.sh pinpoint.py ""
make_runner run_daily.sh daily_update.py ""
make_runner run_daily_yesterday.sh daily_update.py "DAYS_OFFSET=1"
make_runner run_backfill.sh backfill_midnight.py ""

cat <<'DONE'

🎉 セットアップ完了。

次の手順:
  1. env.sh に DATABASE_URL を書く（READMEの手順どおり）
  2. Termux側に戻って（exit と入力）、手動実行:
       bash ~/keirin/run_pinpoint.sh
     ※ 初回は数分〜十数分かかることがあります。実行中は Termux を閉じないでください。
DONE
