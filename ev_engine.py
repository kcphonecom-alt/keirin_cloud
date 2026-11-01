"""
ev_engine.py — 期待値（EV）買い目算出エンジン

keirin_cloud（app_cloud.py）へのブラッシュアップとして追加するモジュール。

役割:
  1. 過去のPRO予想と結果から「予想家 × 券種」ごとの実績を集計し、
     小サンプルを全体平均へ寄せる補正（平滑化）付きで
     買い目1点あたりの推定回収率（EV比）と推定的中率を出す。
  2. 複数予想家が同じ買い目を指す「コンセンサス」の実績も集計する。
  3. 結果未確定のレース（これから走るレース）のPRO予想を展開し、
     EVの高い順に買い目をランキングして予算配分案を返す。

前提データ（app_cloud.py の get_parsed_df と同じ元データ）:
  race_memos  : レースID, 発走時間, 構造化データJSON（予想家/買い目/コメントの配列）
  race_results: レースID, 1着車番, 2着車番, 3着車番, 払戻金JSON

買い目文字列の展開は app_cloud.py の parse_bet_string_detailed_rows を
そのまま流用している（本ファイル後半に同一定義を含む）。
"""

import itertools
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

BET_TYPES = ["3連単", "3連複", "2車単", "ワイド"]


def is_midnight_time(time_str):
    """app_cloud.py と同じミッドナイト判定（発走20:35以降〜4時未満）。"""
    if not time_str or ":" not in str(time_str):
        return False
    try:
        parts = str(time_str).split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        return (hour > 20 or (hour == 20 and minute >= 35) or hour < 4)
    except Exception:
        return False


def parse_bet_string_detailed_rows(
    bet_str, r1, r2, r3, pay_3t, pay_3f, pay_2t, payout_json_raw
):
  bets_3t = set()
  bets_3f = set()
  bets_2t = set()
  bets_w = set()

  official_pay_3t = pay_3t
  official_pay_3f = pay_3f
  official_pay_2t = pay_2t
  wide_dict = {}

  if payout_json_raw:
    try:
      p_list = (
          json.loads(payout_json_raw)
          if isinstance(payout_json_raw, str)
          else payout_json_raw
      )
      if isinstance(p_list, list):
        for item in p_list:
          b_type = item.get("賭け式")
          b_組番 = str(item.get("組番", ""))
          b_amt_str = str(item.get("払戻金", ""))
          b_amt = int(re.sub(r"\D", "", b_amt_str)) if b_amt_str else 0

          if b_type == "3連単" and official_pay_3t == 0:
            official_pay_3t = b_amt
          elif b_type == "3連複" and official_pay_3f == 0:
            official_pay_3f = b_amt
          elif b_type == "2車単" and official_pay_2t == 0:
            official_pay_2t = b_amt
          elif b_type == "ワイド":
            cars = [int(c) for c in re.findall(r"\d+", b_組番)]
            if len(cars) >= 2:
              pair = tuple(sorted([cars[0], cars[1]]))
              wide_dict[pair] = b_amt
    except Exception:
      pass

  if bet_str and isinstance(bet_str, str):
    normalized = bet_str.replace("ー", "-").replace("＝", "=").replace(" ", "")
    raw_lines = re.split(r"[\n,、;]", normalized)

    for line in raw_lines:
      s = line.strip()
      if not s:
        continue
      parts = re.split(r"[/]", s)
      for p in parts:
        p = p.strip()
        if not p:
          continue

        if "=" in p and "-" not in p:
          cars = [int(c) for c in re.findall(r"\d+", p)]
          cars = list(dict.fromkeys(cars))
          if len(cars) >= 3:
            for f, sec, t in itertools.permutations(cars[:3], 3):
              bets_3t.add((f, sec, t))
            for comb in itertools.combinations(cars[:3], 3):
              bets_3f.add(tuple(sorted(comb)))
            for f, sec in itertools.permutations(cars[:3], 2):
              bets_2t.add((f, sec))
            for w_comb in itertools.combinations(cars[:3], 2):
              bets_w.add(tuple(sorted(w_comb)))
            continue
          elif len(cars) == 2:
            bets_2t.add((cars[0], cars[1]))
            bets_2t.add((cars[1], cars[0]))
            bets_w.add(tuple(sorted(cars)))
            continue

        m_complex = re.match(r"^([\d]+)-(\d+)=([\d]+)$", p)
        if m_complex:
          first_group = [int(c) for c in m_complex.group(1)]
          sec_group = [int(c) for c in m_complex.group(2)]
          third_group = [int(c) for c in m_complex.group(3)]

          for f in first_group:
            for s_car in sec_group:
              for t in third_group:
                if len({f, s_car, t}) == 3:
                  bets_3t.add((f, s_car, t))
                  bets_3f.add(tuple(sorted([f, s_car, t])))
                if len({f, t, s_car}) == 3:
                  bets_3t.add((f, t, s_car))
                  bets_3f.add(tuple(sorted([f, t, s_car])))
                if f != s_car:
                  bets_2t.add((f, s_car))
                  bets_w.add(tuple(sorted([f, s_car])))
                if f != t:
                  bets_2t.add((f, t))
                  bets_w.add(tuple(sorted([f, t])))
                if s_car != t:
                  bets_2t.add((s_car, t))
                  bets_w.add(tuple(sorted([s_car, t])))
          continue

        m_axis = re.match(r"^(\d+)-(\d+)=([\d]+)$", p)
        if m_axis:
          first_car = int(m_axis.group(1))
          sec_car = int(m_axis.group(2))
          opponents = [int(c) for c in m_axis.group(3)]

          for opp in opponents:
            if len({first_car, sec_car, opp}) == 3:
              bets_3t.add((first_car, sec_car, opp))
              bets_3t.add((first_car, opp, sec_car))
              bets_3f.add(tuple(sorted([first_car, sec_car, opp])))
              bets_2t.add((first_car, sec_car))
              bets_2t.add((first_car, opp))
              bets_w.add(tuple(sorted([first_car, sec_car])))
              bets_w.add(tuple(sorted([first_car, opp])))
              bets_w.add(tuple(sorted([sec_car, opp])))
          continue

        p_hyphen_eq = p.replace("=", "-")
        tokens = p_hyphen_eq.split("-")

        if len(tokens) == 3:
          g1 = [int(c) for c in tokens[0] if c.isdigit()]
          g2 = [int(c) for c in tokens[1] if c.isdigit()]
          g3 = [int(c) for c in tokens[2] if c.isdigit()]

          for f in g1:
            for sec in g2:
              for t in g3:
                if len({f, sec, t}) == 3:
                  bets_3t.add((f, sec, t))
                  bets_3f.add(tuple(sorted([f, sec, t])))
                  bets_2t.add((f, sec))
                  bets_w.add(tuple(sorted([f, sec])))
          continue

        if len(tokens) == 2:
          g1 = [int(c) for c in tokens[0] if c.isdigit()]
          g2 = [int(c) for c in tokens[1] if c.isdigit()]

          for f in g1:
            for sec in g2:
              if f != sec:
                bets_2t.add((f, sec))
                bets_w.add(tuple(sorted([f, sec])))
          continue

  has_result = pd.notnull(r1) and pd.notnull(r2) and pd.notnull(r3)
  real_3t = (int(r1), int(r2), int(r3)) if has_result else None
  real_3f = tuple(sorted([int(r1), int(r2), int(r3)])) if has_result else None
  real_2t = (int(r1), int(r2)) if has_result else None

  rows = []
  p_3t_got, p_3f_got, p_2t_got, p_w_got = 0, 0, 0, 0

  for b in sorted(bets_3t):
    b_str = f"{b[0]}-{b[1]}-{b[2]}"
    is_hit = has_result and (b == real_3t)
    got = official_pay_3t if is_hit else 0
    p_3t_got += got
    rows.append({
        "券種": "3連単",
        "買い目": b_str,
        "判定": "🎯 的中" if is_hit else "ハズレ",
        "払戻金額": f"{got:,}円" if got > 0 else "-",
        "公式払戻": "",
    })

  for b in sorted(bets_3f):
    b_str = f"{b[0]}={b[1]}={b[2]}"
    is_hit = has_result and (b == real_3f)
    got = official_pay_3f if is_hit else 0
    p_3f_got += got
    rows.append({
        "券種": "3連複",
        "買い目": b_str,
        "判定": "🎯 的中" if is_hit else "ハズレ",
        "払戻金額": f"{got:,}円" if got > 0 else "-",
        "公式払戻": "",
    })

  for b in sorted(bets_2t):
    b_str = f"{b[0]}-{b[1]}"
    is_hit = has_result and (b == real_2t)
    got = official_pay_2t if is_hit else 0
    p_2t_got += got
    rows.append({
        "券種": "2車単",
        "買い目": b_str,
        "判定": "🎯 的中" if is_hit else "ハズレ",
        "払戻金額": f"{got:,}円" if got > 0 else "-",
        "公式払戻": "",
    })

  for b in sorted(bets_w):
    b_str = f"{b[0]}={b[1]}"
    is_hit = False
    got = 0
    if has_result and b in wide_dict:
      is_hit = True
      got = wide_dict[b]
    p_w_got += got
    rows.append({
        "券種": "ワイド",
        "買い目": b_str,
        "判定": "🎯 的中" if is_hit else "ハズレ",
        "払戻金額": f"{got:,}円" if got > 0 else "-",
        "公式払戻": "",
    })

  total_pts = len(bets_3t) + len(bets_3f) + len(bets_2t) + len(bets_w)
  total_cost = max(1, total_pts) * 100
  total_got = p_3t_got + p_3f_got + p_2t_got + p_w_got
  rec_rate = (total_got / total_cost * 100) if total_cost > 0 else 0

  unique_expanded_bets_list = [r["買い目"] for r in rows]

  return (
      rows,
      total_pts,
      total_cost,
      total_got,
      rec_rate,
      p_3t_got,
      p_3f_got,
      p_2t_got,
      p_w_got,
      unique_expanded_bets_list,
  )


# ============================================================
# EVエンジン本体
# ============================================================
import sys


def _yen(value):
    """'1,230円' のような文字列から金額（円）を取り出す。"""
    if value is None:
        return 0
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else 0


def expand_bets(bet_str, result_tuple, payout_raw):
    """買い目文字列を券種別の買い目リストに展開する。

    result_tuple: (1着車番, 2着車番, 3着車番)。未確定レースは (None, None, None)。
    戻り値: [{"券種", "買い目", "的中", "払戻"}] のリスト。
    """
    r1, r2, r3 = result_tuple
    rows = parse_bet_string_detailed_rows(bet_str, r1, r2, r3, 0, 0, 0, payout_raw)[0]
    return [
        {
            "券種": row["券種"],
            "買い目": row["買い目"],
            "的中": row["判定"] == "🎯 的中",
            "払戻": _yen(row.get("払戻金額")),
        }
        for row in rows
    ]


def iter_predictions(memos, results_by_id):
    """race_memos と race_results から (レース, 予想家) 単位の予想を列挙する。"""
    for memo in memos:
        race_id = str(memo.get("レースID", ""))
        res = results_by_id.get(race_id)
        if res is not None:
            result = (res.get("1着車番"), res.get("2着車番"), res.get("3着車番"))
            has_result = all(pd.notnull(x) for x in result)
            payout_raw = res.get("払戻金JSON")
        else:
            result = (None, None, None)
            has_result = False
            payout_raw = None

        plist = memo.get("構造化データJSON")
        if isinstance(plist, str):
            try:
                plist = json.loads(plist)
            except Exception:
                plist = []
        if not isinstance(plist, list):
            continue

        for p in plist:
            if not isinstance(p, dict):
                continue
            yield {
                "レースID": race_id,
                "発走時間": memo.get("発走時間", "-"),
                "予想家": p.get("予想家", "不明"),
                "買い目原文": p.get("買い目", ""),
                "has_result": has_result,
                "result": result,
                "payout_raw": payout_raw,
            }


def _new_stat():
    return {"points": 0, "hit_points": 0, "return_yen": 0, "races": set(), "hit_races": set()}


def _add(stat, hit, payout, race_id):
    stat["points"] += 1
    stat["races"].add(race_id)
    if hit:
        stat["hit_points"] += 1
        stat["return_yen"] += payout
        stat["hit_races"].add(race_id)


def build_history(memos, results_by_id, smooth=50.0):
    """確定済みレースから 予想家×券種 / 券種全体 / コンセンサス の実績を集計する。

    smooth: 平滑化の強さ（仮想点数）。小サンプルの実績を全体平均へ寄せる。
    """
    by_author_type = defaultdict(_new_stat)
    by_type = defaultdict(_new_stat)
    combo_map = {}

    for pred in iter_predictions(memos, results_by_id):
        if not pred["has_result"]:
            continue
        combos = expand_bets(pred["買い目原文"], pred["result"], pred["payout_raw"])
        for c in combos:
            _add(by_author_type[(pred["予想家"], c["券種"])], c["的中"], c["払戻"], pred["レースID"])
            _add(by_type[c["券種"]], c["的中"], c["払戻"], pred["レースID"])
            key = (pred["レースID"], c["券種"], c["買い目"])
            info = combo_map.setdefault(key, {"authors": set(), "hit": False, "payout": 0})
            info["authors"].add(pred["予想家"])
            if c["的中"]:
                info["hit"] = True
                info["payout"] = c["払戻"]

    consensus = defaultdict(_new_stat)
    for (race_id, bet_type, _combo), info in combo_map.items():
        bucket = min(len(info["authors"]), 3)
        _add(consensus[(bet_type, bucket)], info["hit"], info["payout"], race_id)

    return {
        "by_author_type": by_author_type,
        "by_type": by_type,
        "consensus": consensus,
        "smooth": float(smooth),
        "combo_count": len(combo_map),
    }


def _global_ev_yen(global_stat):
    if not global_stat or global_stat["points"] == 0:
        return 0.0
    return global_stat["return_yen"] / global_stat["points"]


def _global_hit_rate(global_stat):
    if not global_stat or global_stat["points"] == 0:
        return 0.0
    return global_stat["hit_points"] / global_stat["points"]


def ev_ratio(stat, global_stat, smooth):
    """平滑化済みの推定回収率（1.0 = 100%）。"""
    if global_stat is None:
        return 0.0
    g_yen = _global_ev_yen(global_stat)
    est_yen = (stat["return_yen"] + smooth * g_yen) / (stat["points"] + smooth)
    return est_yen / 100.0


def hit_rate(stat, global_stat, smooth):
    """平滑化済みの推定的中率（買い目1点あたり）。"""
    if global_stat is None:
        return 0.0
    g_rate = _global_hit_rate(global_stat)
    return (stat["hit_points"] + smooth * g_rate) / (stat["points"] + smooth)


def score_upcoming(
    memos,
    results_by_id,
    stats,
    midnight_only=False,
    bet_types=None,
    odds_map=None,
    min_consensus_points=30,
    from_date=None,
):
    """未確定レースの買い目をEV順にランキングする。

    odds_map: {(レースID, 券種, 買い目): オッズ} を渡すと、
              推定的中率 × 現在オッズ でEVを計算し直す。
    from_date: "YYYYMMDD" を渡すと、その日付以降のレースだけを対象にする
              （結果データが未登録の過去レースを「未来」と誤認しないため）。
    """
    smooth = stats["smooth"]
    by_author_type = stats["by_author_type"]
    by_type = stats["by_type"]
    consensus = stats["consensus"]
    odds_map = odds_map or {}

    combo_map = {}
    for pred in iter_predictions(memos, results_by_id):
        if pred["has_result"]:
            continue
        if from_date and pred["レースID"][:8] < _race_date_threshold(from_date):
            continue
        if midnight_only and not is_midnight_time(pred["発走時間"]):
            continue
        combos = expand_bets(pred["買い目原文"], (None, None, None), None)
        for c in combos:
            if bet_types and c["券種"] not in bet_types:
                continue
            key = (pred["レースID"], c["券種"], c["買い目"])
            info = combo_map.setdefault(
                key, {"authors": set(), "発走時間": pred["発走時間"]}
            )
            info["authors"].add(pred["予想家"])

    rows = []
    for (race_id, bet_type, combo), info in combo_map.items():
        authors = sorted(info["authors"])
        gstat = by_type.get(bet_type)

        # 予想家実績ベース（最もEVの高い予想家を採用）
        best_author, best_ev, best_hr = None, -1.0, 0.0
        for a in authors:
            st = by_author_type.get((a, bet_type))
            if st is None:
                continue
            ev = ev_ratio(st, gstat, smooth)
            if ev > best_ev:
                best_author, best_ev = a, ev
                best_hr = hit_rate(st, gstat, smooth)
        if best_author is None:
            continue

        ev = best_ev
        hr = best_hr
        basis = f"予想家実績:{best_author}"
        sample = by_author_type[(best_author, bet_type)]["points"]

        # コンセンサス実績が十分あれば、予想家実績とブレンドして使う
        # （コンセンサス単独だと同一券種の全買い目が同じEVになり順位が付かないため）
        bucket = min(len(authors), 3)
        cons = consensus.get((bet_type, bucket))
        if bucket >= 2 and cons is not None and cons["points"] >= min_consensus_points:
            cons_ev = ev_ratio(cons, gstat, smooth)
            cons_hr = hit_rate(cons, gstat, smooth)
            ev = 0.6 * cons_ev + 0.4 * ev
            hr = 0.6 * cons_hr + 0.4 * hr
            basis = f"コンセンサス{bucket}名以上" if bucket == 3 else f"コンセンサス{bucket}名"
            sample = cons["points"]

        odds = odds_map.get((race_id, bet_type, combo))
        if odds:
            ev = hr * float(odds)
            basis += "+現在オッズ"

        rows.append({
            "レースID": race_id,
            "発走時間": info["発走時間"],
            "券種": bet_type,
            "買い目": combo,
            "予想家": "、".join(authors),
            "予想家人数": len(authors),
            "推定EV": round(ev, 3),
            "推定回収率%": round(ev * 100, 1),
            "推定的中率%": round(hr * 100, 2),
            "根拠": basis,
            "根拠サンプル点数": sample,
            "オッズ": odds if odds else "",
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(by=["推定EV", "推定的中率%"], ascending=False).reset_index(drop=True)
    return df


def allocate(ranked_df, budget_yen=1500, stake_yen=100, min_ev=1.0, max_points_per_race=None):
    """EVランキングから予算内で買い目配分を作る（1点=stake_yen固定）。"""
    if ranked_df is None or ranked_df.empty:
        return ranked_df, {"points": 0, "amount": 0}
    max_points = budget_yen // stake_yen
    picked = []
    per_race = defaultdict(int)
    for _, row in ranked_df.iterrows():
        if len(picked) >= max_points:
            break
        if row["推定EV"] < min_ev:
            continue
        race_id = row["レースID"]
        if max_points_per_race is not None and per_race[race_id] >= max_points_per_race:
            continue
        per_race[race_id] += 1
        picked.append(row)
    out = pd.DataFrame(picked).reset_index(drop=True) if picked else ranked_df.iloc[0:0].copy()
    if not out.empty:
        out["配分額"] = stake_yen
    return out, {"points": len(out), "amount": len(out) * stake_yen}


def _race_date_threshold(from_date):
    """レースIDの日付は開催「初日」のため、3日間開催の3日目（初日=対象日の2日前）を
    取りこぼさないよう、対象日の2日前までを下限にする。"""
    try:
        base = datetime.strptime(from_date, "%Y%m%d")
        return (base - timedelta(days=2)).strftime("%Y%m%d")
    except (TypeError, ValueError):
        return from_date


def diagnose(memos, results_by_id, from_date=None):
    """候補が0件のときに理由が分かるよう、データ状況を集計する。"""
    races = set()
    unresulted = set()
    for pred in iter_predictions(memos, results_by_id):
        races.add(pred["レースID"])
        if not pred["has_result"]:
            unresulted.add(pred["レースID"])
    threshold = _race_date_threshold(from_date) if from_date else None
    unresulted_from = {
        r for r in unresulted if not threshold or r[:8] >= threshold
    }
    return {
        "races_with_pred": len(races),
        "unresulted_races": len(unresulted),
        "unresulted_from_date": len(unresulted_from),
        "latest_date": max((r[:8] for r in races), default="-"),
    }


def frames_to_inputs(df_memos, df_results):
    """app_cloud.py の DataFrame から iter_predictions 用の入力へ変換する。"""
    memos = df_memos.to_dict("records") if df_memos is not None else []
    results_by_id = {}
    if df_results is not None:
        for rec in df_results.to_dict("records"):
            results_by_id[str(rec.get("レースID", ""))] = rec
    return memos, results_by_id


# ============================================================
# セルフテスト（合成データで計算の正しさを検証）
#   実行: python3 ev_engine.py --selftest
# ============================================================
def _selftest():
    import itertools as _it
    import random

    rng = random.Random(42)

    def payout_json(a, b, c):
        pairs = sorted([tuple(sorted(x)) for x in [(a, b), (a, c), (b, c)]])
        return json.dumps([
            {"賭け式": "3連単", "組番": f"{a}-{b}-{c}", "払戻金": "2500円"},
            {"賭け式": "3連複", "組番": f"{min(a,b,c)}={sorted([a,b,c])[1]}={max(a,b,c)}", "払戻金": "800円"},
            {"賭け式": "2車単", "組番": f"{a}-{b}", "払戻金": "600円"},
        ] + [
            {"賭け式": "ワイド", "組番": f"{x}={y}", "払戻金": "250円"} for (x, y) in pairs
        ], ensure_ascii=False)

    memos, results_by_id = [], {}
    for i in range(240):
        race_id = f"20260101_テスト_{i:03d}"
        u = rng.random()
        if u < 0.5:
            result = (1, 2, 3)
        elif u < 0.6:
            result = (4, 5, 6)
        else:
            result = tuple(rng.sample(range(1, 8), 3))
            if result in [(1, 2, 3), (4, 5, 6)]:
                result = (7, 6, 5)
        preds = [
            {"予想家": "strong", "買い目": "1-2-3", "コメント": ""},
            {"予想家": "weak", "買い目": "4-5-6", "コメント": ""},
        ]
        if i % 3 == 0:
            preds.append({"予想家": "strong2", "買い目": "1-2-3", "コメント": ""})
        memos.append({
            "レースID": race_id, "発走時間": "21:00",
            "構造化データJSON": json.dumps(preds, ensure_ascii=False),
        })
        a, b, c = result
        results_by_id[race_id] = {
            "レースID": race_id, "1着車番": a, "2着車番": b, "3着車番": c,
            "払戻金JSON": payout_json(a, b, c),
        }

    # 未来のレース2件（21:00=ミッドナイト / 19:00=対象外）
    for j, t in enumerate(["21:00", "19:00"]):
        race_id = f"20260102_テスト_{j:03d}"
        memos.append({
            "レースID": race_id, "発走時間": t,
            "構造化データJSON": json.dumps([
                {"予想家": "strong", "買い目": "1-2-3", "コメント": ""},
                {"予想家": "strong2", "買い目": "1-2-3", "コメント": ""},
                {"予想家": "weak", "買い目": "4-5-6", "コメント": ""},
            ], ensure_ascii=False),
        })

    stats = build_history(memos, results_by_id)
    g2 = stats["by_type"]["2車単"]
    ev_strong = ev_ratio(stats["by_author_type"][("strong", "2車単")], g2, stats["smooth"])
    ev_weak = ev_ratio(stats["by_author_type"][("weak", "2車単")], g2, stats["smooth"])
    print(f"strong 2車単 推定EV比: {ev_strong:.3f}")
    print(f"weak   2車単 推定EV比: {ev_weak:.3f}")
    assert ev_strong > 1.5, "strongの EV が想定より低い"
    assert ev_weak < 1.0, "weak の EV が想定より高い"
    assert ev_strong > ev_weak * 2, "strong と weak の分離が不十分"

    ranked = score_upcoming(memos, results_by_id, stats)
    assert not ranked.empty, "未来レースのランキングが空"
    top = ranked.iloc[0]
    print(f"ランキング1位: {top['レースID']} {top['券種']} {top['買い目']} EV={top['推定EV']} 根拠={top['根拠']}")
    assert "strong" in top["予想家"], "1位が strong 系でない"

    ranked_mid = score_upcoming(memos, results_by_id, stats, midnight_only=True)
    assert set(ranked_mid["レースID"]) == {"20260102_テスト_000"}, "ミッドナイト抽出が不一致"

    alloc, summary = allocate(ranked, budget_yen=1500, min_ev=1.0)
    print(f"配分: {summary}")
    assert summary["points"] > 0 and summary["amount"] <= 1500
    print("SELFTEST PASSED")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
