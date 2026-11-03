"""ミッドナイト買い目 回収率チェッカーのコア計算。

race_results のレコード（dictのリスト）を受け取り、指定日のミッドナイト
（発走20:35以降）にその買い目を全レース1点ずつ買った場合の
回収率・金額・根拠レース一覧を返す。Streamlit には依存しない。
"""
import json
import re
from datetime import date, timedelta

_ID_RE = re.compile(r"(\d{4})(\d{2})(\d{2})_(.+?)_(\d+)R_d(\d+)")


def parse_race_id(race_id):
    """レースID（例: 20261031_熊本_7R_d3）から開催日・会場・レース番号を取り出す。"""
    m = _ID_RE.match(str(race_id or ""))
    if not m:
        return None
    base = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return {
        "race_date": base + timedelta(days=int(m.group(6)) - 1),
        "venue": m.group(4),
        "race_no": int(m.group(5)),
        "day": int(m.group(6)),
    }


def is_midnight_time(time_str):
    """アプリ内のミッドナイト定義（20:35以降、または4時前）に合わせる。"""
    if not time_str or ":" not in str(time_str):
        return False
    try:
        parts = str(time_str).split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        return hour > 20 or (hour == 20 and minute >= 35) or hour < 4
    except Exception:
        return False


def combo_key(bet_type, cars):
    """払戻金JSONの組番キーを作る。順不同の券種は小さい順に並べる。"""
    if bet_type in ("ワイド", "2車複"):
        a, b = sorted(cars)
        return f"{a}={b}"
    if bet_type == "2車単":
        return f"{cars[0]}-{cars[1]}"
    if bet_type == "3連複":
        a, b, c = sorted(cars)
        return f"{a}={b}={c}"
    if bet_type == "3連単":
        return f"{cars[0]}-{cars[1]}-{cars[2]}"
    raise ValueError(f"unknown bet_type: {bet_type}")


def payout_map(record):
    """払戻金JSONを {(券種, 組番): 払戻金(100円あたり)} に変換する。"""
    pj = record.get("払戻金JSON")
    entries = json.loads(pj) if isinstance(pj, str) else pj
    out = {}
    if not isinstance(entries, list):
        return out
    for e in entries:
        amt = int(re.sub(r"[^0-9]", "", str(e.get("払戻金", "0"))) or 0)
        out[(e.get("賭け式"), e.get("組番"))] = amt
    return out


def _car(v):
    try:
        return str(int(float(v)))
    except Exception:
        return str(v)


def check(records, target_date, bet_type, cars, stake=100):
    """指定日ミッドナイト全レースに買い目を1点ずつ買った場合の集計を返す。

    戻り値: (summary dict, rows list)
    rows は根拠レース一覧（会場・レース・発走・結果・的中・払戻金）。
    払戻金JSONは100円あたりの金額なので、払戻は stake に比例させる。
    """
    key = combo_key(bet_type, cars)
    rows = []
    for rec in records:
        info = parse_race_id(rec.get("レースID"))
        if not info or info["race_date"] != target_date:
            continue
        if rec.get("is_canceled"):
            continue
        start = rec.get("発走時間")
        if not is_midnight_time(start):
            continue
        if not rec.get("1着車番"):
            continue
        pay = payout_map(rec).get((bet_type, key), 0)
        hit = pay > 0
        rows.append({
            "会場": info["venue"],
            "レース": f"{info['race_no']}R",
            "発走": start,
            "結果": f"{_car(rec.get('1着車番'))}-{_car(rec.get('2着車番'))}-{_car(rec.get('3着車番'))}",
            "的中": hit,
            "払戻金": pay * stake // 100 if hit else 0,
        })
    rows.sort(key=lambda r: (str(r["発走"]), r["会場"], r["レース"]))
    n = len(rows)
    hits = sum(1 for r in rows if r["的中"])
    invested = stake * n
    returned = sum(r["払戻金"] for r in rows)
    summary = {
        "対象レース数": n,
        "的中レース数": hits,
        "的中率": hits / n * 100 if n else 0.0,
        "総投資": invested,
        "総払戻": returned,
        "回収率": returned / invested * 100 if invested else 0.0,
        "収支": returned - invested,
        "組番": key,
    }
    return summary, rows
