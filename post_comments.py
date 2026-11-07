# -*- coding: utf-8 -*-
"""
post_comments.py — レース後コメント（WINTICKET結果ページのインタビュー欄）収集スクリプト。

使い方: daily_update.py と同じフォルダで `python3 post_comments.py [--days 14] [--force]`
  - DB の race_results にある直近 days 日分（開催初日ベース）の確定レースを対象に、
    WINTICKET の結果ページ `?type=race_result_interview` を開き、着順ごとの
    選手コメント（見出し＋本文）を新テーブル player_post_comments へ保存する。
  - レース後コメントは主に S 級の一部レースにだけ付く。付いていないレースは
    そのままスキップする（エラーではない）。
  - 既に保存済みのレースはスキップする（--force で取り直し）。
  - 日本のIPから実行すること（Mac の launchd / 手動実行を想定）。
"""

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright

import daily_update as du

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS player_post_comments (
    "レースID" text,
    "車" integer,
    "選手名" text,
    "着順" integer,
    "見出し" text,
    "コメント" text,
    "取得日時" timestamp DEFAULT now()
)
"""

# 本文の終わりとして扱う目印（ページ下部の共通UI）
_STOP_MARKERS = ("レース情報", "投票シート", "投票ボックス", "着順・払戻金", "映像を観る")


def parse_post_comments(raw_text, riders_by_car=None):
    """
    レース後コメント欄の innerText を
    [{"着順": int, "車": int|None, "選手名": str, "見出し": str, "コメント": str}]
    に変換する純粋関数。

    riders_by_car: {車番: 選手名}（DBの player_stats 由来）。
    渡されれば名前の照合で車番を確定する。無ければ着順ブロック内の
    数字行から車番を推定する。
    """
    if not raw_text or "レース後コメント" not in raw_text:
        return []
    text = raw_text[raw_text.find("レース後コメント"):]
    lines = [l.strip() for l in text.split("\n")]

    # 着順マーカー（"1着" のような行）でブロックに切る
    blocks = []  # (着順, [lines])
    current = None
    for line in lines:
        m = re.match(r"^([1-9])着$", line)
        if m:
            current = (int(m.group(1)), [])
            blocks.append(current)
            continue
        if current is not None:
            current[1].append(line)

    name_to_car = {}
    if riders_by_car:
        name_to_car = {v: k for k, v in riders_by_car.items()}

    results = []
    for rank, blines in blocks:
        name = ""
        car = None
        headline = ""
        body_lines = []
        state = "head"  # head -> headline -> body
        for line in blines:
            if any(line.startswith(mk) for mk in _STOP_MARKERS) and state == "body":
                break
            if state == "head":
                if line in name_to_car:
                    name = line
                    car = name_to_car[line]
                    state = "headline"
                    continue
                if re.match(r"^[1-9]$", line) and car is None:
                    car = int(line)
                    continue
                if line.startswith("――") or line.startswith("——") or line.startswith("― "):
                    headline = line.lstrip("―—- ").strip()
                    state = "body"
                    continue
                # 名前らしい行（2〜6文字・数字や記号を含まない）を名前候補にする
                if not name and 2 <= len(line) <= 6 and re.match(r"^[^\d\s]+$", line) \
                        and line not in ("レース後コメント",):
                    name = line
                    state = "headline"
                    continue
                continue
            if state == "headline":
                if line.startswith("――") or line.startswith("——") or line.startswith("― "):
                    headline = line.lstrip("―—- ").strip()
                    state = "body"
                continue
            body_lines.append(line)
        body = "\n".join([l for l in body_lines if l]).strip()
        if not body and not headline:
            continue
        results.append({
            "着順": rank,
            "車": car,
            "選手名": name,
            "見出し": headline,
            "コメント": body,
        })
    return results


def race_url(venue_jp, base_date_str, day_num, r_num):
    info = du.STADIUM_MASTER[venue_jp]
    base = f"{base_date_str}{info['code']}"
    return (
        f"https://www.winticket.jp/keirin/{info['en']}"
        f"/raceresult/{base}/{day_num}/{r_num}?type=race_result_interview"
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="対象にする過去日数（開催初日ベース）")
    ap.add_argument("--force", action="store_true", help="保存済みレースも取り直す")
    args = ap.parse_args()

    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    min_date = (today - timedelta(days=args.days)).strftime("%Y%m%d")

    conn = du.get_db_connection()
    cursor = conn.cursor()
    cursor.execute(CREATE_TABLE_SQL)
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_post_comments_race ON player_post_comments ("レースID")')
    conn.commit()

    cursor.execute(
        'SELECT "レースID" FROM race_results WHERE "レースID" >= %s', (min_date,)
    )
    race_ids = [r[0] for r in cursor.fetchall()]

    done_ids = set()
    if not args.force:
        cursor.execute('SELECT DISTINCT "レースID" FROM player_post_comments')
        done_ids = {r[0] for r in cursor.fetchall()}

    # レースID -> {車: 選手名}
    riders = {}
    cursor.execute('SELECT "レースID", "車", "選手名" FROM player_stats')
    for rid, car, name in cursor.fetchall():
        try:
            riders.setdefault(rid, {})[int(car)] = name
        except Exception:
            pass

    targets = []
    for rid in race_ids:
        if rid in done_ids:
            continue
        parts = str(rid).split("_")
        if len(parts) < 4:
            continue
        base_date, venue, r_part, d_part = parts[0], parts[1], parts[2], parts[3]
        if venue not in du.STADIUM_MASTER:
            continue
        try:
            r_num = int(re.sub(r"\D", "", r_part))
            day_num = int(re.sub(r"\D", "", d_part))
        except Exception:
            continue
        targets.append((rid, venue, base_date, day_num, r_num))

    print(f"=== レース後コメント収集を開始します（対象 {len(targets)} レース）===")
    saved_races = 0
    saved_rows = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        for rid, venue, base_date, day_num, r_num in targets:
            try:
                url = race_url(venue, base_date, day_num, r_num)
                txt = await du.fetch_rendered_text(page, url, wait_text="レース後コメント")
                parsed = parse_post_comments(txt, riders.get(rid, {}))
                if not parsed:
                    continue
                cursor.execute('DELETE FROM player_post_comments WHERE "レースID" = %s', (rid,))
                for row in parsed:
                    cursor.execute(
                        'INSERT INTO player_post_comments ("レースID", "車", "選手名", "着順", "見出し", "コメント")'
                        " VALUES (%s, %s, %s, %s, %s, %s)",
                        (rid, row["車"], row["選手名"], row["着順"], row["見出し"], row["コメント"]),
                    )
                conn.commit()
                saved_races += 1
                saved_rows += len(parsed)
                print(f"   └ [保存] {rid}: {len(parsed)} 件")
            except Exception as e:
                conn.rollback()
                print(f"   └ [失敗] {rid}: {e}")
        await browser.close()

    cursor.close()
    conn.close()
    print(f"=== 完了（コメントあり {saved_races} レース / {saved_rows} 件を保存）===")


if __name__ == "__main__":
    asyncio.run(main())
