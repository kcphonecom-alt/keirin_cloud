# backfill_midnight.py — ミッドナイト開催の不足分をDBから自動で洗い出し、まとめて取り直す
# GitHub Actions（.github/workflows/backfill_midnight.yml）からの実行を想定。
# 環境変数:
#   BF_FROM       対象の開催初日From (YYYY-MM-DD, 既定 2025-04-01)
#   BF_TO         対象の開催初日To   (YYYY-MM-DD, 既定 2026-10-23)
#   BF_LIMIT_DAYS 1回の実行で処理する開催日の上限 (既定 100)
#   BF_MAX_MINUTES 実行時間の上限・分 (既定 300)。超過したら綺麗に止まる
#   BF_DRY_RUN    1 のとき洗い出しだけして取得しない
import asyncio
import os
import re
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright

import backfill as bf
import daily_update as du

_ID_RE = re.compile(r"(\d{4})(\d{2})(\d{2})_(.+?)_(\d+)R_d(\d+)")


def find_targets(records, from_date, to_date, limit_days):
    """race_results のレコードから、ミッドナイト開催の不足している開催日を洗い出す。

    不足の定義: その開催の最大日レース数（最低7）未満しか結果が無い日、
    または丸ごと欠けている日（1〜3日目）。古い開催から順に返す。
    戻り値: (targets, total) targets は (会場, 初日コード, 日次) のリスト。
    """
    meetings = {}
    for rec in records:
        m = _ID_RE.match(str(rec.get("レースID", "")))
        if not m:
            continue
        base = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        key = (base, m.group(4))
        meetings.setdefault(key, {}).setdefault(int(m.group(6)), []).append(rec)

    found = []
    for (base, venue), days in meetings.items():
        base_date = date(int(base[:4]), int(base[4:6]), int(base[6:8]))
        if not (from_date <= base_date <= to_date):
            continue
        midnight = False
        for rs in days.values():
            ts = sorted(x.get("発走時間") or "" for x in rs)
            if ts and ts[0] >= "20:35":
                midnight = True
        if not midnight:
            continue
        card = max([len(v) for v in days.values()] + [7])
        base_code = base + du.STADIUM_MASTER[venue]["code"]
        for dnum in (1, 2, 3):
            if len(days.get(dnum, [])) < card:
                found.append((base_date, venue, base_code, dnum))
        if 4 in days and len(days[4]) < card:
            found.append((base_date, venue, base_code, 4))
    found.sort()
    total = len(found)
    return [(venue, base_code, dnum) for (_, venue, base_code, dnum) in found[:limit_days]], total


async def main():
    from_date = date.fromisoformat(os.environ.get("BF_FROM", "2025-04-01"))
    to_date = date.fromisoformat(os.environ.get("BF_TO", "2026-10-23"))
    limit_days = int(os.environ.get("BF_LIMIT_DAYS", "100") or "100")
    max_minutes = float(os.environ.get("BF_MAX_MINUTES", "300") or "300")
    dry_run = os.environ.get("BF_DRY_RUN", "") == "1"

    conn = du.get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT "レースID", "発走時間" FROM race_results')
    records = [{"レースID": r[0], "発走時間": r[1]} for r in cur.fetchall()]
    targets, total = find_targets(records, from_date, to_date, limit_days)
    print(f"対象期間 {from_date} 〜 {to_date}: 不足している開催日は全 {total} 件、今回は {len(targets)} 件を処理します")
    for venue, base_code, dnum in targets[:10]:
        print(f"  例: {venue} base={base_code} day={dnum}")
    if dry_run:
        print("DRY RUN のため取得はしません")
        cur.close()
        conn.close()
        return

    started = time.time()
    total_saved = 0
    done_days = 0
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
        for venue, base_code, dnum in targets:
            if (time.time() - started) / 60 > max_minutes:
                print(f"時間上限 ({max_minutes}分) に達したため終了します。残りは次回の実行で続きます")
                break
            total_saved += await bf.process_meeting(page, cur, conn, venue, base_code, dnum, skip_predictions=True)
            done_days += 1
        await browser.close()
    cur.close()
    conn.close()
    print(f"=== backfill_midnight 完了: {done_days} 開催日を処理、結果 {total_saved} レース分を保存 ===")
    remaining = total - done_days
    if remaining > 0:
        print(f"※ 対象期間内の不足はあと約 {remaining} 開催日あります。同じ手順でもう一度実行すると続きから処理されます")


if __name__ == "__main__":
    asyncio.run(main())
