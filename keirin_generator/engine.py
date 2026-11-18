"""生成 + 精算 + 集計エンジン。

- generate: ルール群をレース群に適用して買い目リストを作る
- settle:   買い目を race_results の払戻金JSON と照合する (100円/点)
- aggregate: ルール別に 点数・的中数・投資・払戻・回収率 を集計する。
             前半/後半の分割表示つき (勝者の呪い対策: 片側だけの高回収率を
             「傾向」と呼ばないための最低限の仕掛け)。
"""
from collections import defaultdict
from datetime import date, timedelta

import rules as rules_mod


def generate(races, rule_list, only_date=None):
    """買い目リスト [{rule_id, date, venue, race_no, race_id, bet_type, combo, stake}]"""
    bets = []
    for key in sorted(races):
        race = races[key]
        if only_date and race["date"] != only_date:
            continue
        for rule in rule_list:
            for bet_type, combo in rules_mod.build_bets(rule, race):
                bets.append({
                    "rule_id": rule["id"],
                    "date": race["date"], "venue": race["venue"],
                    "race_no": race["race_no"], "race_id": race["race_id"],
                    "bet_type": bet_type, "combo": combo,
                    "stake": int(rule.get("stake", 100)),
                })
    return bets


def settle(bets, races):
    """結果が入っている買い目に hit / payout を付ける。結果なしは settled=False。"""
    out = []
    for b in bets:
        race = races.get((b["date"], b["venue"], b["race_no"]))
        res = race["result"] if race else None
        nb = dict(b)
        if not res or res.get("canceled"):
            nb.update({"settled": False, "hit": False, "payout": 0})
        else:
            pay = res["payouts"].get((b["bet_type"], b["combo"]), 0)
            nb.update({"settled": True, "hit": pay > 0, "payout": pay})
        out.append(nb)
    return out


def _half_of(d, split):
    return "first" if d <= split else "second"


def aggregate(settled_bets):
    """ルール別集計。分割日はルールごとに、そのルールの精算済みレース日の
    中央値 (その日以前が前半)。

    戻り値: {rule_id: {"total": {...}, "first": {...}, "second": {...},
                       "split_date": str, "races": int}}
    各 {...} は points / hits / invested / returned / roi_pct。
    """
    settled = [b for b in settled_bets if b["settled"]]

    def blank():
        return {"points": 0, "hits": 0, "invested": 0, "returned": 0}

    # ルールごとの分割日
    dates_by_rule = defaultdict(set)
    for b in settled:
        dates_by_rule[b["rule_id"]].add(b["date"])
    splits = {}
    for rid, ds in dates_by_rule.items():
        ds = sorted(ds)
        splits[rid] = ds[len(ds) // 2] if ds else None

    per = defaultdict(lambda: {"total": blank(), "first": blank(),
                               "second": blank(), "races": set()})
    for b in settled:
        halves = ["total", _half_of(b["date"], splits[b["rule_id"]])]
        for h in halves:
            s = per[b["rule_id"]][h]
            s["points"] += 1
            s["hits"] += 1 if b["hit"] else 0
            s["invested"] += b["stake"]
            s["returned"] += b["payout"] * b["stake"] // 100
        per[b["rule_id"]]["races"].add((b["date"], b["venue"], b["race_no"]))

    out = {}
    for rid, d in per.items():
        for h in ("total", "first", "second"):
            s = d[h]
            s["roi_pct"] = round(s["returned"] / s["invested"] * 100, 1) if s["invested"] else None
        out[rid] = {"total": d["total"], "first": d["first"],
                    "second": d["second"], "split_date": splits.get(rid),
                    "races": len(d["races"])}
    return out


def format_table(agg, rule_list):
    names = {r["id"]: r.get("name", r["id"]) for r in rule_list}
    lines = ["| ルール | 対象R | 点数 | 的中 | 投資 | 払戻 | 回収率 | 前半 回収率(n) | 後半 回収率(n) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for rid in [r["id"] for r in rule_list]:
        if rid not in agg:
            lines.append(f"| {names[rid]} | 0 | 0 | 0 | 0 | 0 | - | - | - |")
            continue
        a = agg[rid]
        t, f, s = a["total"], a["first"], a["second"]
        lines.append(
            f"| {names[rid]} | {a['races']} | {t['points']} | {t['hits']} | "
            f"{t['invested']:,} | {t['returned']:,} | "
            f"{t['roi_pct'] if t['roi_pct'] is not None else '-'} | "
            f"{f['roi_pct'] if f['roi_pct'] is not None else '-'} ({f['points']}) | "
            f"{s['roi_pct'] if s['roi_pct'] is not None else '-'} ({s['points']}) |")
    return "\n".join(lines)
