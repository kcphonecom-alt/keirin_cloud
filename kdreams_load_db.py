# -*- coding: utf-8 -*-
"""
kdreams_load_db.py — Kドリームス記者予想（印）を Supabase の新テーブル
kdreams_marks へ投入するスクリプト。

使い方: このスクリプトと同じフォルダに kdreams_marks_YYYYMM.csv
（202411〜202610）を置いて `python3 kdreams_load_db.py` を実行するだけ。
  - DATABASE_URL は環境変数 → 同フォルダの env.sh → daily_update モジュール
    の順で探す（Mac の ~/keirin_v3 でそのまま動く想定）。
  - テーブルは自動作成され、再実行しても上書き（upsert）されるので安全。
  - レースIDはアプリ形式（例: 20241104_青森_1R_d1）に変換して保存する。
    元のKドリームスIDも kd_race_id 列に残す。
"""

import csv
import glob
import json
import os
import re
import sys

# Kドリームスの会場コード → アプリ側の会場名（daily_update.STADIUM_MASTER と一致。
# アプリ未収録の5会場は一般名で入れる：DBに相手がいないだけで害はない）
CODE_TO_VENUE = {
    "11": "函館", "12": "青森", "13": "いわき平", "21": "弥彦", "22": "前橋",
    "23": "取手", "24": "宇都宮", "25": "大宮", "26": "西武園", "27": "京王閣",
    "28": "立川", "31": "松戸", "34": "川崎", "35": "平塚", "36": "小田原",
    "37": "伊東温泉", "38": "静岡", "42": "名古屋", "43": "岐阜", "44": "大垣",
    "45": "豊橋", "46": "富山", "47": "松阪", "48": "四日市", "51": "福井",
    "53": "奈良", "55": "和歌山", "56": "岸和田", "61": "玉野", "62": "広島",
    "63": "防府", "71": "高松", "73": "小松島", "74": "高知", "75": "松山",
    "81": "小倉", "83": "久留米", "84": "武雄", "85": "佐世保", "86": "別府",
    "87": "熊本",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kdreams_marks (
    race_id    TEXT PRIMARY KEY,
    kd_race_id TEXT,
    venue      TEXT,
    reporter   TEXT,
    marks      JSONB,
    honmei     INT,
    taikou     INT,
    created_at TIMESTAMP DEFAULT now()
)
"""


def find_database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    here = os.path.dirname(os.path.abspath(__file__))
    env_sh = os.path.join(here, "env.sh")
    if os.path.exists(env_sh):
        m = re.search(r'DATABASE_URL=["\']?([^\s"\']+)', open(env_sh, encoding="utf-8").read())
        if m:
            return m.group(1)
    try:
        sys.path.insert(0, here)
        import daily_update as du
        return getattr(du, "DATABASE_URL", None)
    except Exception:
        return None


def to_app_race_id(kd_rid):
    """KドリームスID（会場2+初日8+日次2+R4）→ アプリID（初日_会場_NR_d日次）。
    R番号が100以上（同日・同会場の別シリーズ戦。例: 防府のチャレンジ一般）は
    アプリ側のID体系に対応付けできないため None を返してスキップする。"""
    code, start, day, rnum = kd_rid[:2], kd_rid[2:10], int(kd_rid[10:12]), int(kd_rid[12:16])
    venue = CODE_TO_VENUE.get(code)
    if venue is None or rnum >= 100:
        return None
    return f"{start}_{venue}_{rnum}R_d{day}"


def load_rows(data_dir):
    races = {}
    files = sorted(glob.glob(os.path.join(data_dir, "kdreams_marks_*.csv")))
    files = [f for f in files if "done" not in os.path.basename(f)]
    if not files:
        print(f"[エラー] {data_dir} に kdreams_marks_*.csv がありません")
        sys.exit(1)
    for path in files:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rid = r["レースID"]
                e = races.setdefault(rid, {"reporter": r["記者"], "marks": {}})
                e["marks"][int(r["車"])] = r["印"]
    rows = []
    skipped = 0
    for kd_rid, e in races.items():
        app_id = to_app_race_id(kd_rid)
        if app_id is None:
            skipped += 1
            continue
        marks = {str(k): v for k, v in sorted(e["marks"].items())}
        honmei = next((int(k) for k, v in marks.items() if v == "◎"), None)
        taikou = next((int(k) for k, v in marks.items() if v == "○"), None)
        rows.append((app_id, kd_rid, CODE_TO_VENUE[kd_rid[:2]], e["reporter"],
                     json.dumps(marks, ensure_ascii=False), honmei, taikou))
    print(f"CSV {len(files)} ファイル / {len(races):,} レース → 投入対象 {len(rows):,} 件（変換不可 {skipped} 件）")
    return rows


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--dry-run", action="store_true", help="DBに書かず、変換結果の確認だけ")
    args = ap.parse_args()

    rows = load_rows(args.data_dir)
    print("変換サンプル:", rows[0][0], "/", rows[len(rows) // 2][0], "/", rows[-1][0])
    if args.dry_run:
        print("[dry-run] DBへは書きませんでした")
        return

    url = find_database_url()
    if not url:
        print("[致命的エラー] DATABASE_URL が見つかりません。env.sh のあるフォルダで実行してください。")
        sys.exit(1)
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO kdreams_marks (race_id, kd_race_id, venue, reporter, marks, honmei, taikou)
                VALUES %s
                ON CONFLICT (race_id) DO UPDATE SET
                    kd_race_id = EXCLUDED.kd_race_id, venue = EXCLUDED.venue,
                    reporter = EXCLUDED.reporter, marks = EXCLUDED.marks,
                    honmei = EXCLUDED.honmei, taikou = EXCLUDED.taikou
                """,
                rows, page_size=1000,
            )
            cur.execute("SELECT COUNT(*) FROM kdreams_marks")
            total = cur.fetchone()[0]
        conn.commit()
        print(f"✅ 投入完了。kdreams_marks の総件数: {total:,}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
