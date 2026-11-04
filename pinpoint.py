# pinpoint.py — PRO予想だけを取りに行く緊急用スクリプト（開催自動検知版）
# 使い方: daily_update.py と同じフォルダで `python3 pinpoint.py`
#   実行した日（JST）の開催を全会場から自動検知して、その日のPRO予想を保存する。
#   日付が変われば翌日の開催を自動で対象にするので、毎晩同じファイルでよい。
#   ※0:00〜3:59に実行した場合は前日分を対象にする（ミッドナイト開催の続きとして扱う）。
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright

import daily_update as du

# 自動検知が万一0件だった場合の保険（2026-11-04分のみ検証済みの対象）
FALLBACK_DATE = "2026-11-04"
FALLBACK_TARGETS = [
    ("取手", "2026110423", 1),
    ("大宮", "2026110425", 1),
    ("青森", "2026110312", 2),
    ("豊橋", "2026110245", 3),
    ("別府", "2026110486", 1),
    ("静岡", "2026110238", 3),
    ("奈良", "2026110253", 3),
    ("武雄", "2026110284", 3),
]


def target_date():
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    d = now.date()
    if now.hour < 4:
        d = d - timedelta(days=1)
    return d


async def discover(page, today):
    found = []
    for venue_jp, info in du.STADIUM_MASTER.items():
        matched_base = None
        matched_day = None
        for days_ago in range(6):
            start_dt = today - timedelta(days=days_ago)
            test_base = f"{start_dt.strftime('%Y%m%d')}{info['code']}"
            day_num = (today - start_dt).days + 1
            if day_num < 1 or day_num > 7:
                continue
            url = (
                f"https://www.winticket.jp/keirin/{info['en']}"
                f"/racecard/{test_base}/{day_num}/1"
            )
            txt = await du.fetch_rendered_text(page, url)
            if not du.is_valid_race_page(txt, venue_jp):
                continue
            probe = du.parse_winticket_detailed_score(txt, venue_jp, "probe", None)
            if not probe:
                continue
            matched_base = test_base
            matched_day = day_num
            break
        if matched_base:
            print(f"🏟️ {venue_jp}（{matched_day}日目 / 初日コード: {matched_base}）")
            found.append((venue_jp, matched_base, matched_day))
    return found


async def main():
    today = target_date()
    print(f"=== pinpoint 対象日: {today.isoformat()} ===")
    conn = du.get_db_connection()
    cur = conn.cursor()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        targets = await discover(page, today)
        if not targets and today.isoformat() == FALLBACK_DATE:
            print("⚠ 自動検知が0件のため、2026-11-04分の固定対象で実行します")
            targets = FALLBACK_TARGETS
        print(f"=== 対象: {len(targets)} 会場 ===")
        for venue_jp, base, day in targets:
            info = du.STADIUM_MASTER[venue_jp]
            en, code = info["en"], info["code"]
            base_date = base[:8]
            print(f"== {venue_jp} (base={base}, day={day}) ==")
            for r in range(1, 13):
                race_id = f"{base_date}_{venue_jp}_{r}R_d{day}"
                old_id = f"{base_date}{code}_{en}_{r}R_d{day}"
                card = await du.fetch_rendered_text(
                    page, f"https://www.winticket.jp/keirin/{en}/racecard/{base}/{day}/{r}"
                )
                if not du.is_valid_race_page(card, venue_jp):
                    print(f"  {r}R: 出走表が取れない → この会場はここまで")
                    break
                start_time = du.extract_start_time(card)
                pred = await du.fetch_rendered_text(
                    page, f"https://www.winticket.jp/keirin/{en}/predictions/{base}/{day}/{r}"
                )
                preds = du.parse_pro_predictions_html(pred)
                if not preds:
                    print(f"  {r}R: 予想0件（文字数:{len(pred or '')}）→ 書き込みなし")
                    continue
                cur.execute(
                    'DELETE FROM race_memos WHERE "レースID" IN (%s, %s)', (race_id, old_id)
                )
                lines = [
                    f"【{x['予想家']} ({x['投稿時間']})】\n{x['コメント']}\n買い目: {x['買い目']}\n"
                    for x in preds
                ]
                raw = preds[0].get("買い目", "")
                c1, c2, c3 = du.parse_bet_to_rows(raw)
                cur.execute(
                    """
                    INSERT INTO race_memos (
                        "レースID", "PRO予想テキスト", "構造化データJSON", "発走時間",
                        "買い目原文", "Pro予想買い目1", "Pro予想買い目2", "Pro予想買い目3"
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        race_id,
                        "\n".join(lines),
                        json.dumps(preds, ensure_ascii=False),
                        start_time,
                        raw,
                        c1,
                        c2,
                        c3,
                    ),
                )
                conn.commit()
                print(f"  {r}R: 予想{len(preds)}件を保存 ✓")
        await browser.close()
    cur.close()
    conn.close()
    print("=== pinpoint 完了 ===")


if __name__ == "__main__":
    asyncio.run(main())
