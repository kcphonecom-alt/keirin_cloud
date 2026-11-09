# odds_watch.py — EV監視ワーカー（Mac / Android の日本IP側で定期実行する）
# 使い方: ev_watch.py と同じフォルダで `python3 odds_watch.py`
#   1. 今日のおすすめ買い目の「監視タスク」をDBから読む
#      （無ければ、その日の保存履歴の最新買い目から自動で作る）
#   2. 発走が近いレースから WINTICKET のオッズページへ行き、監視中の
#      買い目の実オッズだけを拾う
#   3. 的中率（固定）× 実オッズ で EV を計算し、判定つきでDBに残す
#   判定はアプリ「🎯 今日のおすすめ買い目」の「📡 EV監視」に表示される。
#
# 設計メモ（ai-goofish-monitor の借用）:
#   - タスクごとにログを出す（失敗の切り分けをログでできるように）
#   - 取得と表示を分ける（このスクリプトは取得と判定だけ）
#   - 任意で ntfy 通知: 環境変数 EV_WATCH_NTFY にトピック名を入れると、
#     判定が「見送り」に変わった時だけ https://ntfy.sh/<topic> へ送る
#
# daily_update.py への依存は STADIUM_MASTER と接続情報だけに絞る
# （Mac側コピーの版差で壊れた前例があるため、取得関数は自前で持つ）。
import asyncio
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ev_watch


def get_db_connection():
    """DATABASE_URL は 環境変数 → 同フォルダの env.sh → daily_update の順で探す。"""
    url = os.environ.get("DATABASE_URL")
    if not url:
        env_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.sh")
        if os.path.exists(env_sh):
            with open(env_sh, encoding="utf-8", errors="ignore") as f:
                m = re.search(r"DATABASE_URL=['\"]?([^'\"\n]+)", f.read())
                if m:
                    url = m.group(1).strip()
    if url:
        import psycopg2
        return psycopg2.connect(url)
    import daily_update as du
    return du.get_db_connection()


def stadium_master():
    import daily_update as du
    return du.STADIUM_MASTER


def target_date(now=None):
    """対象日（JST）。0:00〜3:59 は前日分として扱う（pinpoint.py と同じ）。"""
    now = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    d = now.date()
    if now.hour < 4:
        d = d - timedelta(days=1)
    return d


def race_datetime(target_d, start_time):
    """発走日時（JST）。0〜3時台の発走は翌日未明のものとして扱う。"""
    if not start_time or ":" not in str(start_time):
        return None
    try:
        h, m = [int(x) for x in str(start_time).split(":")[:2]]
    except Exception:
        return None
    base = datetime(target_d.year, target_d.month, target_d.day,
                    tzinfo=ZoneInfo("Asia/Tokyo"))
    dt = base + timedelta(hours=h, minutes=m)
    if h < 4:
        dt = dt + timedelta(days=1)
    return dt


def odds_url(race_id):
    """レースIDから WINTICKET のオッズページURLを作る。作れなければ None。"""
    parts = str(race_id).split("_")
    if len(parts) < 4:
        return None
    start_date, venue, r_part, d_part = parts[0], parts[1], parts[2], parts[3]
    master = stadium_master()
    if venue not in master:
        return None
    info = master[venue]
    r_num = re.sub(r"\D", "", r_part)
    day_num = re.sub(r"\D", "", d_part)
    base = f"{start_date}{info['code']}"
    return f"https://www.winticket.jp/keirin/{info['en']}/odds/{base}/{day_num}/{r_num}"


async def fetch_odds_text(page, url, bet_type):
    """オッズページ専用の取得（自前・daily_update 非依存）。

    券種はURLでは切り替わらず、ページ内のタブで切り替わる
    （2026-11-10 実物確認）。開いた後に券種タブを押してから本文を取る。
    """
    try:
        if page.url != url:
            await page.goto(url, wait_until="networkidle", timeout=20000)
    except Exception:
        pass
    try:
        await page.get_by_text(bet_type, exact=True).first.click(timeout=4000)
    except Exception:
        pass
    try:
        await page.wait_for_timeout(1200)
        return await page.evaluate("() => document.body.innerText")
    except Exception:
        return ""


def notify_ntfy(message):
    topic = os.environ.get("EV_WATCH_NTFY", "").strip()
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": "EV watch (keirin)"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"   [通知失敗] {e}")


def sync_tasks_if_empty(conn, target_str, now_str):
    """監視タスクが無い日は、保存履歴の最新買い目から自動作成する。"""
    tasks = ev_watch.load_watch_tasks(conn, target_str)
    if tasks:
        return tasks
    cur = conn.cursor()
    cur.execute(
        "SELECT picks_json FROM today_picks_history "
        "WHERE target_date = %s ORDER BY id DESC LIMIT 1",
        (target_str,),
    )
    row = cur.fetchone()
    if not row:
        return []
    try:
        picks = json.loads(row[0])
    except Exception:
        return []
    ev_watch.sync_watch_tasks(conn, target_str, picks, now_str)
    return ev_watch.load_watch_tasks(conn, target_str)


async def main():
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    tdate = target_date(now)
    target_str = tdate.strftime("%Y%m%d")
    now_str = now.strftime("%Y-%m-%d %H:%M")
    print(f"=== EV監視 {now_str}（対象日 {target_str}）===")

    conn = get_db_connection()
    tasks = sync_tasks_if_empty(conn, target_str, now_str)
    if not tasks:
        print("監視タスクなし（今日のおすすめ買い目がまだ無い日です）")
        conn.close()
        return

    # 発走を過ぎた（+15分）レースは対象から外す。発走時刻が読めないものは見る。
    live_tasks = []
    for t in tasks:
        rdt = race_datetime(tdate, t.get("発走時間"))
        if rdt is not None and now > rdt + timedelta(minutes=15):
            continue
        live_tasks.append(t)
    print(f"監視タスク {len(tasks)} 件（発走前 {len(live_tasks)} 件）")
    if not live_tasks:
        conn.close()
        print("=== EV監視 終了（全レース発走済み）===")
        return

    from playwright.async_api import async_playwright

    # レース単位にまとめて、オッズページは1レース1回だけ開く
    by_race = {}
    for t in live_tasks:
        by_race.setdefault(t["レースID"], []).append(t)

    prev_latest = ev_watch.latest_snapshots(conn, target_str)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ja-JP", timezone_id="Asia/Tokyo")
        page = await context.new_page()
        for race_id, race_tasks in by_race.items():
            url = odds_url(race_id)
            label = f"{race_tasks[0]['会場']} {race_id.split('_')[2]}"
            if not url:
                print(f"[{label}] URLが作れずスキップ")
                continue
            # 券種ごとにタブを切り替えて本文を取り直す
            texts = {}
            for bet_type in {t["券種"] for t in race_tasks}:
                texts[bet_type] = await fetch_odds_text(page, url, bet_type)
            for t in race_tasks:
                text = texts.get(t["券種"], "")
                odds = ev_watch.parse_odds_text(text, t["券種"], t["買い目"])
                if odds is None:
                    print(f"[{label} {t['券種']} {t['買い目']}] オッズ未検出")
                    continue
                saved = ev_watch.record_snapshot(
                    conn, target_str, race_id, t["券種"], t["買い目"],
                    t["推定的中率%"], odds, now_str)
                ev_pct = saved["ev"] * 100 if saved["ev"] is not None else 0.0
                print(
                    f"[{label} {t['券種']} {t['買い目']}] 実オッズ {odds:.1f}倍 "
                    f"（分岐 {t['損益分岐オッズ']:.2f}倍） EV {ev_pct:+.0f}% "
                    f"→ {saved['verdict']}"
                )
                key = (race_id, t["券種"], t["買い目"])
                prev = prev_latest.get(key)
                if saved["verdict"] == "見送り" and (
                        not prev or prev["判定"] != "見送り"):
                    notify_ntfy(
                        f"{label} {t['券種']} {t['買い目']}: 実オッズ {odds:.1f}倍で"
                        f"損益分岐 {t['損益分岐オッズ']:.2f}倍を割り、見送り判定になりました"
                    )
        await browser.close()
    conn.close()
    print("=== EV監視 終了 ===")


if __name__ == "__main__":
    asyncio.run(main())
