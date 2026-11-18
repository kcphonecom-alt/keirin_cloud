#!/usr/bin/env python3
"""競輪版 買い目ジェネレーター CLI。

使い方:
  python3 gen.py demo [--rules rules_sample.json]
      キャッシュ済みデータで全ルールを過去データに適用し、精算して
      ルール別集計表 (前半/後半つき) を表示する。

  python3 gen.py forward --date 2026-11-18 [--rules ...] [--fetch]
      指定日のレースについて入力が揃う範囲で買い目を生成し、
      forward/YYYY-MM-DD.json に保存する。

  python3 gen.py settle --date 2026-11-18 [--fetch]
      forward ファイルを結果と照合して精算し、ルール別集計を表示する。
      結果は同じファイルの settlement に書き戻す。

  --fetch: Supabase から race_memos / external_predictions / race_results を
           再取得して data/ を更新する (player_stats は大きいので --fetch-all のときだけ)。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine
import inputs
import rules as rules_mod

FETCH_HELPER = os.path.expanduser(
    "~/workspace/custom-connectors/custom.supabase-keirin-db/bin/supabase_fetch.py")
JST = timezone(timedelta(hours=9))


def fetch(light_only=True):
    tables = ["race_memos", "external_predictions", "race_results"]
    if not light_only:
        tables.append("player_stats")
    for t in tables:
        out = os.path.join(inputs.DATA_DIR, f"{t}.json")
        print(f"fetching {t} ...", file=sys.stderr)
        subprocess.run([sys.executable, FETCH_HELPER, "--table", t,
                        "--out", out, "--max-pages", "0"],
                       check=True, capture_output=True)


def cmd_demo(args):
    rule_list = rules_mod.load_rules(args.rules)
    races = inputs.build_race_inputs()
    bets = engine.generate(races, rule_list)
    settled = engine.settle(bets, races)
    agg = engine.aggregate(settled)
    n_races = len({(b["date"], b["venue"], b["race_no"]) for b in bets})
    n_settled = len([b for b in settled if b["settled"]])
    print(f"# 実演: {len(rule_list)} ルール / 買い目生成 {len(bets)} 点 "
          f"({n_races} レース) / 精算済み {n_settled} 点")
    print("# 前半/後半の分割日はルールごとに精算済みレース日の中央値 "
          "(詳細は report_demo.json の split_date)\n")
    print(engine.format_table(agg, rule_list))
    out = os.path.join(HERE, "report_demo.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(JST).isoformat(),
                   "aggregate": agg,
                   "bets": settled}, f, ensure_ascii=False, indent=1)
    print(f"\n詳細: {out}")
    return 0


def cmd_forward(args):
    if args.fetch or args.fetch_all:
        fetch(light_only=not args.fetch_all)
    rule_list = rules_mod.load_rules(args.rules)
    races = inputs.build_race_inputs()
    bets = engine.generate(races, rule_list, only_date=args.date)
    path = os.path.join(HERE, "forward", f"{args.date}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"date": args.date,
           "generated_at": datetime.now(JST).isoformat(),
           "rules_file": os.path.basename(args.rules),
           "bets": bets}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    per_rule = {}
    for b in bets:
        per_rule[b["rule_id"]] = per_rule.get(b["rule_id"], 0) + 1
    print(f"{args.date}: {len(bets)} 点を生成 -> {path}")
    for rid, n in sorted(per_rule.items()):
        print(f"  {rid}: {n} 点")
    if not bets:
        print("  (入力が揃ったレースがありませんでした)")
    return 0


def cmd_settle(args):
    if args.fetch or args.fetch_all:
        fetch(light_only=not args.fetch_all)
    path = os.path.join(HERE, "forward", f"{args.date}.json")
    if not os.path.exists(path):
        print(f"forward ファイルがありません: {path}", file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    rule_list = rules_mod.load_rules(args.rules)
    races = inputs.build_race_inputs()
    settled = engine.settle(doc["bets"], races)
    agg = engine.aggregate(settled)
    n_done = len([b for b in settled if b["settled"]])
    print(f"{args.date}: 精算済み {n_done} / {len(settled)} 点\n")
    print(engine.format_table(agg, rule_list))
    doc["settlement"] = {"settled_at": datetime.now(JST).isoformat(),
                         "settled_points": n_done, "aggregate": agg}
    doc["bets"] = settled
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"\n書き戻し: {path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="競輪版 買い目ジェネレーター")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("demo", "forward", "settle"):
        p = sub.add_parser(name)
        p.add_argument("--rules", default=os.path.join(HERE, "rules_sample.json"))
        p.add_argument("--fetch", action="store_true")
        p.add_argument("--fetch-all", action="store_true")
        if name in ("forward", "settle"):
            p.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    return {"demo": cmd_demo, "forward": cmd_forward,
            "settle": cmd_settle}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
