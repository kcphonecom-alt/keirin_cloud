"""「EV監視」— 今日のおすすめ買い目を実オッズで見張る仕組み。

発想はフリマ監視ツール（ai-goofish-monitor）の設計の借用:
- 監視タスクを買い目ごとに1件作り、それぞれが自分の条件
  （的中率＝損益分岐オッズ）を持つ
- 定期実行のワーカーが実オッズを取りに行き、条件と照らして判定し、
  履歴（スナップショット）を残す
- 画面は最新の判定と履歴を表示するだけ（取得と表示を分ける）

このモジュールは「判定の計算」と「DBの読み書き」だけを持つ。
実オッズの取得（Playwright）は odds_watch.py（Mac側）,
表示は app_cloud.py（今日のおすすめ買い目ページ）が担当する。

判定ルール（過去の検証の学びを反映）:
- 的中率はオッズを見る前に固定する（ev_v2 / PRO履歴由来の推定的中率）。
  実オッズで的中率を動かさない。
- EV = 的中率 × 実オッズ − 1。損益分岐オッズ = 1 ÷ 的中率。
- EV < 0（実オッズが分岐未満）なら「見送り」。
- ワイドは実オッズ1.5倍未満は、EVがプラスでも「見送り」
  （2026-11-05の振り返りの学び: ワイド1.5倍未満は買わない）。
"""

import json
import re
from datetime import datetime

WIDE_MIN_ODDS = 1.5  # ワイドの下限（これ未満はEVプラスでも見送り）

CREATE_TASKS_SQL = """
    CREATE TABLE IF NOT EXISTS ev_watch_tasks (
        id SERIAL PRIMARY KEY,
        target_date TEXT,
        race_id TEXT,
        venue TEXT,
        start_time TEXT,
        kind TEXT,
        bet_type TEXT,
        combo TEXT,
        hit_prob_pct REAL,
        breakeven_odds REAL,
        created_at TEXT,
        UNIQUE (target_date, race_id, bet_type, combo)
    )
"""

CREATE_SNAPSHOTS_SQL = """
    CREATE TABLE IF NOT EXISTS ev_odds_snapshots (
        id SERIAL PRIMARY KEY,
        target_date TEXT,
        race_id TEXT,
        bet_type TEXT,
        combo TEXT,
        checked_at TEXT,
        live_odds REAL,
        ev REAL,
        verdict TEXT,
        source TEXT
    )
"""

CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_ev_snap_lookup "
    "ON ev_odds_snapshots (target_date, race_id, bet_type, combo, id)"
)


def breakeven_odds(hit_prob_pct):
    """損益分岐オッズ = 1 ÷ 的中率。的中率が不正なら None。"""
    try:
        p = float(hit_prob_pct)
    except (TypeError, ValueError):
        return None
    if p <= 0 or p > 100:
        return None
    return 100.0 / p


def expected_value(hit_prob_pct, live_odds):
    """EV（1点あたりの期待収支率） = 的中率 × 実オッズ − 1。"""
    try:
        p = float(hit_prob_pct) / 100.0
        o = float(live_odds)
    except (TypeError, ValueError):
        return None
    if p <= 0 or o <= 0:
        return None
    return p * o - 1.0


def verdict_for(bet_type, hit_prob_pct, live_odds):
    """実オッズへの判定を返す: 買い圏内 / 見送り / 判定不能。"""
    if live_odds is None:
        return "判定不能"
    ev = expected_value(hit_prob_pct, live_odds)
    if ev is None:
        return "判定不能"
    if bet_type == "ワイド" and float(live_odds) < WIDE_MIN_ODDS:
        return "見送り"
    return "買い圏内" if ev >= 0 else "見送り"


def ensure_tables(conn):
    cur = conn.cursor()
    cur.execute(CREATE_TASKS_SQL)
    cur.execute(CREATE_SNAPSHOTS_SQL)
    cur.execute(CREATE_INDEX_SQL)
    conn.commit()


def sync_watch_tasks(conn, target_date, picks, now_str=None):
    """今日のおすすめ買い目（本命／おすすめ）から監視タスクを作る。

    picks は today_picks.select_picks / 保存履歴の picks_json と同じ形:
    {"本命": [...], "おすすめ": [...]}、各買い目は
    レースID / 会場 / 発走時間 / 券種 / 買い目 / 推定的中率% を持つ。
    すでに同じ買い目のタスクがあれば作り直さない（的中率は初回の値を固定）。
    戻り値: 新しく作ったタスク数。
    """
    ensure_tables(conn)
    now_str = now_str or datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = conn.cursor()
    created = 0
    for kind in ("本命", "おすすめ"):
        for p in picks.get(kind, []) or []:
            hit = p.get("推定的中率%")
            be = breakeven_odds(hit)
            if be is None:
                continue
            cur.execute(
                "INSERT INTO ev_watch_tasks "
                "(target_date, race_id, venue, start_time, kind, bet_type, "
                " combo, hit_prob_pct, breakeven_odds, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (target_date, race_id, bet_type, combo) "
                "DO NOTHING",
                (target_date, str(p.get("レースID", "")),
                 str(p.get("会場", "")), str(p.get("発走時間", "")),
                 kind, str(p.get("券種", "")), str(p.get("買い目", "")),
                 float(hit), round(be, 4), now_str),
            )
            created += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return created


def load_watch_tasks(conn, target_date):
    """その日の監視タスクを発走時間順に返す（dictのリスト）。"""
    ensure_tables(conn)
    cur = conn.cursor()
    cur.execute(
        "SELECT race_id, venue, start_time, kind, bet_type, combo, "
        "hit_prob_pct, breakeven_odds FROM ev_watch_tasks "
        "WHERE target_date = %s ORDER BY start_time, race_id",
        (target_date,),
    )
    cols = ["レースID", "会場", "発走時間", "区分", "券種", "買い目",
            "推定的中率%", "損益分岐オッズ"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def record_snapshot(conn, target_date, race_id, bet_type, combo,
                    hit_prob_pct, live_odds, checked_at, source="odds_watch"):
    """実オッズ1件を判定つきで保存し、保存した内容を dict で返す。"""
    ensure_tables(conn)
    ev = expected_value(hit_prob_pct, live_odds)
    verdict = verdict_for(bet_type, hit_prob_pct, live_odds)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ev_odds_snapshots "
        "(target_date, race_id, bet_type, combo, checked_at, live_odds, "
        " ev, verdict, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (target_date, race_id, bet_type, combo, checked_at,
         float(live_odds) if live_odds is not None else None,
         round(ev, 4) if ev is not None else None, verdict, source),
    )
    conn.commit()
    return {"live_odds": live_odds, "ev": ev, "verdict": verdict,
            "checked_at": checked_at}


def latest_snapshots(conn, target_date):
    """(レースID, 券種, 買い目) → 最新スナップショット dict の対応を返す。"""
    ensure_tables(conn)
    cur = conn.cursor()
    cur.execute(
        "SELECT race_id, bet_type, combo, checked_at, live_odds, ev, verdict "
        "FROM ev_odds_snapshots WHERE target_date = %s ORDER BY id ASC",
        (target_date,),
    )
    out = {}
    for race_id, bet_type, combo, checked_at, live_odds, ev, verdict in cur.fetchall():
        out[(race_id, bet_type, combo)] = {
            "確認時刻": checked_at, "実オッズ": live_odds,
            "EV": ev, "判定": verdict,
        }
    return out


def snapshot_history(conn, target_date, race_id, bet_type, combo):
    """1買い目分のオッズ推移を古い順に返す。"""
    ensure_tables(conn)
    cur = conn.cursor()
    cur.execute(
        "SELECT checked_at, live_odds, ev, verdict FROM ev_odds_snapshots "
        "WHERE target_date = %s AND race_id = %s AND bet_type = %s "
        "AND combo = %s ORDER BY id ASC",
        (target_date, race_id, bet_type, combo),
    )
    return [{"確認時刻": c, "実オッズ": o, "EV": e, "判定": v}
            for c, o, e, v in cur.fetchall()]


# --- オッズページのテキスト解析 -------------------------------------------
# WINTICKETのオッズページ本文（innerText）から、指定の買い目の倍率を拾う。
# 実ページの並びに合わせた検証は odds_watch.py 側のセルフテストで行う。

def _combo_numbers(combo):
    return [int(x) for x in re.findall(r"\d+", str(combo))]


def parse_odds_text(text, bet_type, combo):
    """オッズページ本文から (券種, 買い目) の倍率を返す。見つからなければ None。

    実ページの並び（2026-11-10 未明に WINTICKET 松山9R の実物で確認済み）:
    - 人気順リストで1行が「順位 X番 - Y番 倍率」（2車単、順序あり）
    - ワイドは「順位 X番 = Y番 下限〜上限」の範囲表示で、下限を返す
      （実際に買える保証のある一番低い倍率で判定するのが安全なため）
    - 券種はタブ切り替えで、取得側がタブを選んだ状態の本文を渡す前提
    予備として、単純な "1-2 3.1" 形式の本文にも対応する。
    """
    if not text:
        return None
    nums = _combo_numbers(combo)
    if len(nums) != 2:
        return None
    # 実ページ形式（「X番 - Y番 倍率」／「X番 = Y番 下限〜上限」）
    if bet_type == "2車単":
        for m in re.finditer(
                r"(\d+)番\s*-\s*(\d+)番[\s　]*([0-9]+\.[0-9]+)", text):
            if (int(m.group(1)), int(m.group(2))) == (nums[0], nums[1]):
                return float(m.group(3))
    elif bet_type == "ワイド":
        for m in re.finditer(
                r"(\d+)番\s*=\s*(\d+)番[\s　]*([0-9]+\.[0-9]+)", text):
            pair = (int(m.group(1)), int(m.group(2)))
            if pair == (nums[0], nums[1]) or pair == (nums[1], nums[0]):
                return float(m.group(3))  # 範囲の下限
    # 予備形式（"1-2 3.1" のような単純な並び）
    section = _section_for_bet_type(text, bet_type)
    if section is None:
        return None
    sep = "-" if bet_type == "2車単" else "="
    targets = {f"{nums[0]}{sep}{nums[1]}"}
    if bet_type == "ワイド":
        targets.add(f"{nums[1]}{sep}{nums[0]}")
    for target in targets:
        # 同じ行に倍率
        m = re.search(re.escape(target) + r"[ \t　]*([0-9]+\.[0-9])", section)
        if m:
            return float(m.group(1))
        # 次の行に倍率
        m = re.search(re.escape(target) + r"\s*\n\s*([0-9]+\.[0-9])", section)
        if m:
            return float(m.group(1))
    return None


_BET_TYPE_HEADINGS = ["3連単", "3連複", "2車単", "2車複", "ワイド", "2枠単", "2枠複"]


def _section_for_bet_type(text, bet_type):
    """本文を券種見出しで区切り、指定券種の範囲だけを返す。

    見出しが1つも見つからない本文（券種別ページ等）は全文を対象にする。
    """
    heads = []
    for h in _BET_TYPE_HEADINGS:
        for m in re.finditer(re.escape(h), text):
            heads.append((m.start(), h))
    if not heads:
        return text
    heads.sort()
    for i, (pos, h) in enumerate(heads):
        if h == bet_type:
            end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
            return text[pos:end]
    return None


if __name__ == "__main__":
    # 計算のセルフテスト
    assert abs(breakeven_odds(26.0) - 3.846) < 0.01
    assert breakeven_odds(0) is None and breakeven_odds(120) is None
    assert abs(expected_value(26.0, 3.1) - (-0.194)) < 0.001
    assert abs(expected_value(61.8, 2.3) - 0.4214) < 0.001
    assert verdict_for("2車単", 26.0, 3.1) == "見送り"      # 分岐3.84未満
    assert verdict_for("2車単", 26.0, 4.5) == "買い圏内"
    assert verdict_for("ワイド", 61.8, 2.3) == "買い圏内"
    assert verdict_for("ワイド", 80.0, 1.4) == "見送り"      # ワイド1.5倍未満ルール
    assert verdict_for("2車単", 26.0, None) == "判定不能"

    # パーサーのセルフテスト（実ページ形式・2026-11-10 松山9Rの実例ベース）
    real = (
        "2車単\n"
        "1 2番 - 1番 2.4\n"
        "2 1番 - 2番 2.8\n"
        "3 1番 - 5番 14.8\n"
        "24 1番 - 4番 121.5\n"
    )
    assert parse_odds_text(real, "2車単", "2-1") == 2.4   # 順序あり
    assert parse_odds_text(real, "2車単", "1-2") == 2.8
    assert parse_odds_text(real, "2車単", "1-5") == 14.8
    assert parse_odds_text(real, "2車単", "5-1") is None
    real_w = (
        "ワイド\n"
        "1 1番 = 2番 1.2〜1.4\n"
        "2 2番 = 3番 2.4〜3.2\n"
        "21 4番 = 7番 85.4〜111.9\n"
    )
    assert parse_odds_text(real_w, "ワイド", "1=2") == 1.2  # 範囲の下限
    assert parse_odds_text(real_w, "ワイド", "2=1") == 1.2  # 順序なし
    assert parse_odds_text(real_w, "ワイド", "4=7") == 85.4
    assert parse_odds_text("オッズはありません", "2車単", "1-2") is None

    # 予備形式（想定レイアウト2種）
    sample = "2車単\n1-2 3.1\n1-3 5.4\n2-1 8.9\nワイド\n1=2 2.3\n1=3 3.0\n"
    assert parse_odds_text(sample, "2車単", "1-2") == 3.1
    assert parse_odds_text(sample, "2車単", "2-1") == 8.9
    assert parse_odds_text(sample, "ワイド", "1=2") == 2.3
    assert parse_odds_text(sample, "ワイド", "2=1") == 2.3  # ワイドは順序なし
    assert parse_odds_text(sample, "2車単", "3-2") is None
    sample2 = "2車単\n1-2\n3.1\n1-3\n5.4\n"
    assert parse_odds_text(sample2, "2車単", "1-3") == 5.4
    print("ev_watch self-test OK")
