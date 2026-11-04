"""
backtest_ev.py — EV戦略の walk-forward バックテスト

目的:
  ev_engine (v1) や ev_v2 の「推定EVが高い買い目を買う」戦略が、
  過去データの後付けではなく時系列の未来に対してどれだけ回収できたかを測る。

方法 (walk-forward):
  開催日 D ごとに、その日より前の確定レースだけで履歴を構築し、
  D のレース (エンジン側には未確定として見せる) をスコアリングして
  予算配分まで再現し、実際の結果・払戻と突き合わせて集計する。
  未来の情報は一切使わない。

使い方:
  python3 backtest_ev.py --selftest     # 合成データで動作検証
  DATABASE_URL=... python3 backtest_ev.py --from-db --min-train 200 --budget 1500

出力:
  戦略 (EVしきい値) ごとの 総投資 / 総払戻 / 回収率 / 的中点数 / 対象日数。
  「全候補を全部買う」ベースラインも併記する (75%前後に寄るはずの対照群)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

import pandas as pd

import ev_engine

ORDERED_TYPES = ("3連単", "2車単", "2枠単")


# ---------------------------------------------------------------------------
# 払戻の解決
# ---------------------------------------------------------------------------
def payout_map(payout_raw) -> dict:
    """払戻金JSON -> {(券種, 車番タプル): 払戻金(円/100円)}"""
    out = {}
    if not payout_raw:
        return out
    try:
        items = json.loads(payout_raw) if isinstance(payout_raw, str) else payout_raw
    except Exception:
        return out
    if not isinstance(items, list):
        return out
    for item in items:
        bet_type = item.get("賭け式")
        cars = tuple(int(c) for c in re.findall(r"\d+", str(item.get("組番", ""))))
        if not cars:
            continue
        amt_str = str(item.get("払戻金", ""))
        amt = int(re.sub(r"\D", "", amt_str)) if amt_str else 0
        key_cars = cars if bet_type in ORDERED_TYPES else tuple(sorted(cars))
        out[(bet_type, key_cars)] = amt
    return out


def _combo_key(bet_type: str, combo: str) -> tuple:
    cars = tuple(int(c) for c in re.findall(r"\d+", str(combo)))
    return cars if bet_type in ORDERED_TYPES else tuple(sorted(cars))


def realized_payout_yen(bet_type: str, combo: str, result_rec: dict) -> int:
    """候補1点 (100円) の実際の払戻。外れは0。"""
    pm = payout_map(result_rec.get("払戻金JSON"))
    return pm.get((bet_type, _combo_key(bet_type, combo)), 0)


# ---------------------------------------------------------------------------
# v1 スコアラのアダプタ (backtest 共通インターフェースに合わせる)
#   scorer(train_memos, train_results_by_id, test_memos) -> ranked DataFrame
#   ranked は少なくとも [レースID, 券種, 買い目, 推定EV] を持つこと。
# ---------------------------------------------------------------------------
def v1_scorer(midnight_only=False, bet_types=None):
    def _score(train_memos, train_results_by_id, test_memos):
        if not train_memos:
            return pd.DataFrame()
        stats = ev_engine.build_history(train_memos, train_results_by_id)
        # テスト側は結果を渡さない = エンジンには未確定レースとして見える
        return ev_engine.score_upcoming(
            test_memos, {}, stats,
            midnight_only=midnight_only, bet_types=bet_types,
        )
    return _score


def v2_scorer(players_by_race, midnight_only=False, bet_types=None):
    """ev_v2 用アダプタ。player_stats は出走表 (レース前に確定する情報) なので、
    テスト対象レースの選手データを使ってもリークにはならない。"""
    import ev_v2

    def _score(train_memos, train_results_by_id, test_memos):
        if not train_memos or not test_memos:
            return pd.DataFrame()
        stats = ev_v2.build_history_v2(train_memos, train_results_by_id)
        # 選手データはテスト対象レース分だけに絞る (絞らないと、結果を渡していない
        # 学習レースまで「未来レース」と誤認して候補に入ってしまう)
        test_ids = {str(m.get("レースID", "")) for m in test_memos}
        filtered_players = {rid: players_by_race[rid] for rid in test_ids if rid in players_by_race}
        from_date = min((rid[:8] for rid in test_ids), default=None)
        return ev_v2.score_upcoming_v2(
            test_memos, {}, filtered_players, stats,
            midnight_only=midnight_only, bet_types=bet_types, from_date=from_date,
        )
    return _score


# ---------------------------------------------------------------------------
# walk-forward 本体
# ---------------------------------------------------------------------------
def _race_date(race_id: str) -> str:
    return str(race_id)[:8]


def walk_forward(memos, results_by_id, scorer,
                 thresholds=(0.8, 1.0, 1.2, 1.5),
                 budget_per_day=1500, stake_yen=100,
                 min_train_races=100, max_days=None, progress=None):
    """開催日単位で walk-forward 評価する。

    戻り値: (summary_df, picks_df)
      summary_df: 戦略ごとの集計 (ベースライン含む)
      picks_df:   採用した全ピックの明細 (戦略, 開催日, レースID, 券種, 買い目, 推定EV, 払戻)
    """
    # 結果が確定しているレースだけをテスト対象にできる (答え合わせが必要なため)
    resulted_ids = {
        rid for rid, rec in results_by_id.items()
        if all(pd.notnull(rec.get(c)) for c in ("1着車番", "2着車番", "3着車番"))
    }
    memo_by_date = defaultdict(list)
    train_pool = []  # (race_id, memo) を日付順に積む
    all_memos_by_id = defaultdict(list)
    for memo in memos:
        all_memos_by_id[str(memo.get("レースID", ""))].append(memo)

    dates = sorted({_race_date(rid) for rid in resulted_ids if rid in all_memos_by_id})
    # 日付ごとに、その日のメモと、それ以前の学習データを切り分ける
    memos_by_date = defaultdict(list)
    for memo in memos:
        memos_by_date[_race_date(memo.get("レースID", ""))].append(memo)

    totals = {
        t: {"cost": 0, "pay": 0, "points": 0, "hits": 0, "days": 0}
        for t in thresholds
    }
    base = {"cost": 0, "pay": 0, "points": 0, "hits": 0, "days": 0}
    picks_rows = []
    train_memos: list = []
    train_results: dict = {}
    tested_days = 0

    for date in dates:
        todays_ids = {rid for rid in resulted_ids if _race_date(rid) == date}
        todays_memos = [m for m in memos_by_date.get(date, [])
                        if str(m.get("レースID", "")) in todays_ids]
        # 学習データが足りない初期期間はスキップ (ただし学習には積む)
        if len(train_results) >= min_train_races and todays_memos:
            if max_days is not None and tested_days >= max_days:
                pass
            else:
                ranked = scorer(train_memos, train_results, todays_memos)
                tested_days += 1
                if progress:
                    progress(date, len(train_results), 0 if ranked is None else len(ranked))
                if ranked is not None and not ranked.empty:
                    # ベースライン: その日の全候補を1点ずつ
                    base["days"] += 1
                    for _, row in ranked.iterrows():
                        rec = results_by_id[str(row["レースID"])]
                        pay = realized_payout_yen(row["券種"], row["買い目"], rec)
                        base["cost"] += stake_yen
                        base["pay"] += pay
                        base["points"] += 1
                        base["hits"] += 1 if pay > 0 else 0
                    # しきい値戦略: ev_engine.allocate で日予算内に配分
                    for t in thresholds:
                        alloc, _summary = ev_engine.allocate(
                            ranked, budget_yen=budget_per_day,
                            stake_yen=stake_yen, min_ev=float(t),
                        )
                        if alloc is None or alloc.empty:
                            continue
                        totals[t]["days"] += 1
                        for _, row in alloc.iterrows():
                            rec = results_by_id[str(row["レースID"])]
                            pay = realized_payout_yen(row["券種"], row["買い目"], rec)
                            totals[t]["cost"] += stake_yen
                            totals[t]["pay"] += pay
                            totals[t]["points"] += 1
                            totals[t]["hits"] += 1 if pay > 0 else 0
                            picks_rows.append({
                                "戦略EV>=": t, "開催日": date,
                                "レースID": row["レースID"], "券種": row["券種"],
                                "買い目": row["買い目"], "推定EV": row["推定EV"],
                                "払戻": pay,
                            })
        # 今日の分を学習データへ (この順序が walk-forward の要)
        for memo in memos_by_date.get(date, []):
            train_memos.append(memo)
        for rid in todays_ids | {str(m.get("レースID", "")) for m in memos_by_date.get(date, [])}:
            if rid in results_by_id:
                train_results[rid] = results_by_id[rid]

    rows = []
    for t in thresholds:
        s = totals[t]
        rows.append({
            "戦略": f"EV>={t}",
            "対象日数": s["days"], "点数": s["points"],
            "総投資": s["cost"], "総払戻": s["pay"],
            "回収率%": round(s["pay"] / s["cost"] * 100, 1) if s["cost"] else 0.0,
            "的中点数": s["hits"],
        })
    rows.append({
        "戦略": "全候補ベースライン",
        "対象日数": base["days"], "点数": base["points"],
        "総投資": base["cost"], "総払戻": base["pay"],
        "回収率%": round(base["pay"] / base["cost"] * 100, 1) if base["cost"] else 0.0,
        "的中点数": base["hits"],
    })
    return pd.DataFrame(rows), pd.DataFrame(picks_rows)


# ---------------------------------------------------------------------------
# DBから読む (DATABASE_URL がある環境でのみ)
# ---------------------------------------------------------------------------
def load_from_db(with_players=False):
    import os
    import pandas as _pd
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL が設定されていません")
    import psycopg2 as _pg
    conn = _pg.connect(url)
    df_memos = _pd.read_sql("SELECT * FROM race_memos", conn)
    df_results = _pd.read_sql("SELECT * FROM race_results", conn)
    players_by_race = None
    if with_players:
        import ev_v2
        df_players = _pd.read_sql("SELECT * FROM player_stats", conn)
        players_by_race = ev_v2.frames_to_players(df_players)
    conn.close()
    memos, results_by_id = ev_engine.frames_to_inputs(df_memos, df_results)
    return memos, results_by_id, players_by_race


# ---------------------------------------------------------------------------
# セルフテスト (合成データ)
# ---------------------------------------------------------------------------
def _selftest():
    import random
    rng = random.Random(7)

    def payout_json(a, b, c):
        pairs = sorted([tuple(sorted(x)) for x in [(a, b), (a, c), (b, c)]])
        return json.dumps([
            {"賭け式": "3連単", "組番": f"{a}-{b}-{c}", "払戻金": "2500円"},
            {"賭け式": "3連複", "組番": f"{min(a,b,c)}={sorted([a,b,c])[1]}={max(a,b,c)}", "払戻金": "800円"},
            {"賭け式": "2車単", "組番": f"{a}-{b}", "払戻金": "600円"},
        ] + [{"賭け式": "ワイド", "組番": f"{x}={y}", "払戻金": "250円"} for (x, y) in pairs],
            ensure_ascii=False)

    memos, results_by_id, player_records = [], {}, []
    # 30日 x 12レース。strong は 1-2-3 (結果の50%)、weak は 4-5-6 (10%) を指す
    for d in range(30):
        date = f"202601{d+1:02d}"
        for i in range(12):
            race_id = f"{date}_テスト_{i:03d}"
            for car in range(1, 8):
                player_records.append({
                    "レースID": race_id, "車": car, "選手名": f"選手{car}",
                    "AI": "◎" if car == 1 else "-",
                    "競走得点": 100 - car * 2, "勝率": max(0.02, 0.30 - car * 0.04),
                    "2連対率": 0.4, "3連対率": 0.5, "脚質": "両",
                })
            u = rng.random()
            if u < 0.5:
                result = (1, 2, 3)
            elif u < 0.6:
                result = (4, 5, 6)
            else:
                result = tuple(rng.sample(range(1, 8), 3))
                if result in [(1, 2, 3), (4, 5, 6)]:
                    result = (7, 6, 5)
            memos.append({
                "レースID": race_id, "発走時間": "21:00",
                "構造化データJSON": json.dumps([
                    {"予想家": "strong", "買い目": "1-2-3", "コメント": ""},
                    {"予想家": "weak", "買い目": "4-5-6", "コメント": ""},
                ], ensure_ascii=False),
            })
            a, b, c = result
            results_by_id[race_id] = {
                "レースID": race_id, "1着車番": a, "2着車番": b, "3着車番": c,
                "払戻金JSON": payout_json(a, b, c),
            }

    summary, picks = walk_forward(
        memos, results_by_id, v1_scorer(),
        thresholds=(1.0,), budget_per_day=1500, min_train_races=100,
    )
    print(summary.to_string(index=False))
    strat = summary[summary["戦略"] == "EV>=1.0"].iloc[0]
    base_row = summary[summary["戦略"] == "全候補ベースライン"].iloc[0]
    assert strat["点数"] > 0, "EV戦略のピックが0"
    assert strat["回収率%"] > base_row["回収率%"], "EV戦略がベースライン以下"
    print(f"picks: {len(picks)} rows")

    # v2 スコアラでも walk-forward が回ること
    import ev_v2
    players_by_race = ev_v2.players_to_by_race(player_records)
    summary2, picks2 = walk_forward(
        memos, results_by_id, v2_scorer(players_by_race),
        thresholds=(1.0,), budget_per_day=1500, min_train_races=100,
    )
    print(summary2.to_string(index=False))
    assert summary2[summary2["戦略"] == "EV>=1.0"].iloc[0]["点数"] > 0, "v2のピックが0"
    print("BACKTEST SELFTEST PASSED")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--from-db", action="store_true")
    ap.add_argument("--min-train", type=int, default=200)
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--midnight-only", action="store_true")
    ap.add_argument("--engine", choices=["v1", "v2"], default="v1")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
    elif args.from_db:
        memos, results_by_id, players_by_race = load_from_db(with_players=(args.engine == "v2"))
        if args.engine == "v2":
            scorer = v2_scorer(players_by_race, midnight_only=args.midnight_only)
        else:
            scorer = v1_scorer(midnight_only=args.midnight_only)
        summary, picks = walk_forward(
            memos, results_by_id, scorer,
            budget_per_day=args.budget, min_train_races=args.min_train,
            progress=lambda d, n, k: print(f"  {d}: train={n} candidates={k}", file=sys.stderr),
        )
        print(summary.to_string(index=False))
        if args.out and not picks.empty:
            picks.to_csv(args.out, index=False)
            print(f"picks -> {args.out}")
    else:
        ap.print_help()
