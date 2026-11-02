# pinpoint.py — 巡回検知をせず、指定した開催のPRO予想だけを取りに行く緊急用スクリプト
# 使い方: daily_update.py と同じフォルダで `python3 pinpoint.py`
# 対象を変えたいときは TARGETS の行を編集（会場, 初日コード, 日次, 最大レース数）
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright

import daily_update as du

TARGETS = [
    ("熊本", "2026103187", 3, 7),
    ("武雄", "2026103184", 3, 12),
]


async def main():
    conn = du.get_db_connection()
    cur = conn.cursor()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        for venue_jp, base, day, max_r in TARGETS:
            info = du.STADIUM_MASTER[venue_jp]
            en, code = info["en"], info["code"]
            base_date = base[:8]
            print(f"== {venue_jp} (base={base}, day={day}) ==")
            for r in range(1, max_r + 1):
                race_id = f"{base_date}_{venue_jp}_{r}R_d{day}"
                old_id = f"{base_date}{code}_{en}_{r}R_d{day}"
                card = await du.fetch_rendered_text(
                    page, f"https://www.winticket.jp/keirin/{en}/racecard/{base}/{day}/{r}"
                )
                if not du.is_valid_race_page(card, venue_jp):
                    print(f"  {r}R: 出走表が取れない（初日コード/日次が違う可能性）→ この会場はここまで")
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
