# backfill.py — 指定した過去開催の「出走表・PRO予想・結果」を取り直す補完スクリプト
# 使い方: daily_update.py と同じフォルダで `python3 backfill.py`
# 対象を変えたいときは TARGETS の行を編集（会場, 初日コード, 日次, 最大レース数）
# ※ backfill_midnight.py（Actions用・自動洗い出し）からも process_meeting を共用する
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright
from psycopg2.extras import execute_values

import daily_update as du

TARGETS = [
    ("青森", "2026102412", 2, 12),
    ("川崎", "2026102434", 3, 12),
    ("青森", "2026102412", 3, 12),
    ("久留米", "2026102783", 1, 12),
    ("大垣", "2026102744", 1, 12),
    ("久留米", "2026102783", 2, 12),
    ("大垣", "2026102744", 2, 12),
    ("久留米", "2026102783", 3, 12),
    ("大垣", "2026102744", 3, 12),
]


async def fetch_pred_text(page, url):
    """古い daily_update.py（wait_text 引数なし）でも動くようにする互換ラッパー。"""
    try:
        return await du.fetch_rendered_text(page, url, wait_text="PROユーザー予想")
    except TypeError:
        return await du.fetch_rendered_text(page, url)


async def process_meeting(page, cur, conn, venue_jp, base, day, max_r=12):
    """1開催日分（会場×日次）の出走表・予想・結果を取り直す。保存できた結果レース数を返す。"""
    info = du.STADIUM_MASTER[venue_jp]
    en, code = info["en"], info["code"]
    base_date = base[:8]
    print(f"== {venue_jp} (base={base}, day={day}) ==")
    saved_results = 0
    for r in range(1, max_r + 1):
        race_id = f"{base_date}_{venue_jp}_{r}R_d{day}"
        old_id = f"{base_date}{code}_{en}_{r}R_d{day}"
        try:
            card = await du.fetch_rendered_text(
                page, f"https://www.winticket.jp/keirin/{en}/racecard/{base}/{day}/{r}"
            )
            if not du.is_valid_race_page(card, venue_jp):
                print(f"  {r}R: 出走表が取れない → この会場はここまで")
                break
            start_time = du.extract_start_time(card)

            players = du.parse_winticket_detailed_score(card, venue_jp, race_id, start_time)
            if players:
                cur.execute(
                    'DELETE FROM player_stats WHERE "レースID" IN (%s, %s)',
                    (race_id, old_id),
                )
                execute_values(
                    cur,
                    """
                    INSERT INTO player_stats (
                        "レースID", "競輪場", "枠", "車", "選手名", "AI", "競走得点",
                        "S", "H", "B", "脚質", "1着", "2着", "3着", "3着外", "着外",
                        "差", "マ", "勝率", "2連対率", "3連対率", "ギヤ倍率", "コメント", "メモ", "発走時間"
                    ) VALUES %s
                    """,
                    players,
                )

            pred = await fetch_pred_text(
                page,
                f"https://www.winticket.jp/keirin/{en}/predictions/{base}/{day}/{r}",
            )
            preds = du.parse_pro_predictions_html(pred)
            if preds:
                cur.execute(
                    'DELETE FROM race_memos WHERE "レースID" IN (%s, %s)',
                    (race_id, old_id),
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

            result_text = await du.fetch_rendered_text(
                page, f"https://www.winticket.jp/keirin/{en}/raceresult/{base}/{day}/{r}"
            )
            res_data = du.parse_race_result_page(result_text, race_id, start_time)
            if res_data:
                cur.execute(
                    'DELETE FROM race_results WHERE "レースID" IN (%s, %s)',
                    (race_id, old_id),
                )
                cur.execute(
                    """
                    INSERT INTO race_results (
                        "レースID", "1着車番", "1着選手名", "2着車番", "2着選手名", "3着車番", "3着選手名", "払戻金JSON", "発走時間"
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    res_data,
                )
                saved_results += 1
            conn.commit()
            res_flag = "有" if res_data else "未確定/無"
            print(f"  {r}R: [完了] 出走:{len(players)}人, 予想:{len(preds)}件, 結果:{res_flag} ✓")
        except Exception as e:
            conn.rollback()
            print(f"  {r}R: エラー: {e}")
    return saved_results


async def main():
    conn = du.get_db_connection()
    cur = conn.cursor()
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
        for venue_jp, base, day, max_r in TARGETS:
            await process_meeting(page, cur, conn, venue_jp, base, day, max_r)
        await browser.close()
    cur.close()
    conn.close()
    print("=== backfill 完了 ===")


if __name__ == "__main__":
    asyncio.run(main())
