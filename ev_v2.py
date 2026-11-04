"""
ev_v2.py — EV買い目算出エンジン v2 (確率モデル版)

v1 (ev_engine.py) との違い:
  v1 は「その買い目パターンの過去払戻実績」をブレンドして推定回収率を出していた。
  v2 は player_stats (競走得点・勝率・連対率・AI印・脚質) から
  「今日のレースの各車の1着確率」を作り、PRO予想の支持で補正してから
  Harville式で券種ごとの組み合わせ確率へ展開する。
  EV = モデル確率 x オッズ (オッズが無い買い目は、券種ごとの
  「的中時平均払戻」x モデル確率 を推定回収率として使う)。

出力 DataFrame の列は v1 の score_upcoming と互換にしてあるので、
app_cloud.py 側は ev_engine.allocate をそのまま流用できる。

前提データ:
  player_stats: レースID, 車, 選手名, AI(印), 競走得点, 勝率, 2連対率, 3連対率, 脚質
  race_memos / race_results: ev_engine と同じ形式
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict

import pandas as pd

import ev_engine

BET_TYPES = ev_engine.BET_TYPES  # ["3連単", "3連複", "2車単", "ワイド"]

# WINTICKET出走表のAI印 -> 強さスコア (0..1)。"-" や空は中立 0.5。
AI_MARK_SCORE = {"◎": 1.0, "○": 0.8, "▲": 0.6, "注": 0.55, "△": 0.4, "×": 0.15, "✕": 0.15, "-": 0.5, "": 0.5, "元": 0.5}
POSITION_CREDIT = (1.0, 0.6, 0.35)


def _num(value, default=None):
    """'25.0' / 25.0 / '-' / None を float に寄せる。"""
    if value is None:
        return default
    try:
        if pd.isnull(value):
            return default
    except Exception:
        pass
    s = str(value).strip().replace("%", "")
    if s in ("", "-", "－"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _rate(value, default=0.0):
    """勝率などを 0..1 に寄せる。25 / '25.0' は 0.25 として扱う。"""
    v = _num(value, None)
    if v is None:
        return default
    return v / 100.0 if v > 1.0 else v


# ---------------------------------------------------------------------------
# 履歴の集計: PRO重み / 車番事前分布 / 券種別の的中時平均払戻
# ---------------------------------------------------------------------------
def build_history_v2(memos, results_by_id, smooth_points=100.0):
    """確定済みレースから v2 用の統計を作る。

    戻り値 dict:
      author_weight: {予想家: 重み} 券種別に「平滑化的中率 / 全体的中率」を出し、
                     試行点数で加重平均したもの。平均的なPRO=1.0、cap [0.3, 3.0]
      car_win_prior: {車番: 1着率} 全確定レースの実績 (平滑化付き)
      car_top3_prior: {車番: 3着内率}
      mean_payout: {券種: 的中時の平均払戻金(円)} オッズが無い場合のEV推定用
      races: 学習に使った確定レース数
    """
    v1 = ev_engine.build_history(memos, results_by_id, smooth=smooth_points)
    by_author_type = v1["by_author_type"]
    by_type = v1["by_type"]

    # PRO重み
    weight_sum = defaultdict(float)
    weight_val = defaultdict(float)
    for (author, bet_type), stat in by_author_type.items():
        g = by_type.get(bet_type)
        if not g or g["points"] == 0:
            continue
        hr = ev_engine.hit_rate(stat, g, smooth_points)
        g_hr = ev_engine.hit_rate(g, g, smooth_points)
        if g_hr <= 0:
            continue
        w = min(3.0, max(0.3, hr / g_hr))
        weight_val[author] += w * stat["points"]
        weight_sum[author] += stat["points"]
    author_weight = {
        a: weight_val[a] / weight_sum[a] for a in weight_sum if weight_sum[a] > 0
    }

    # 車番の事前分布と券種別平均払戻 (結果から直接数える)
    win_count = defaultdict(int)
    top3_count = defaultdict(int)
    pos_count = {1: defaultdict(int), 2: defaultdict(int), 3: defaultdict(int)}
    n_races = 0
    payout_sum = defaultdict(float)
    payout_cnt = defaultdict(int)
    for race_id, rec in results_by_id.items():
        r = [_num(rec.get(c)) for c in ("1着車番", "2着車番", "3着車番")]
        if any(x is None for x in r):
            continue
        n_races += 1
        win_count[int(r[0])] += 1
        for pos_idx, x in enumerate(r, start=1):
            top3_count[int(x)] += 1
            pos_count[pos_idx][int(x)] += 1
        # 的中時平均払戻: そのレースで実際に払い戻された券種別金額
        pm = {}
        raw = rec.get("払戻金JSON")
        if raw:
            import json as _json
            import re as _re
            try:
                items = _json.loads(raw) if isinstance(raw, str) else raw
                for item in items if isinstance(items, list) else []:
                    amt = int(_re.sub(r"\D", "", str(item.get("払戻金", ""))) or 0)
                    if amt > 0:
                        pm.setdefault(item.get("賭け式"), []).append(amt)
            except Exception:
                pm = {}
        for bet_type, amounts in pm.items():
            for amt in amounts:
                payout_sum[bet_type] += amt
                payout_cnt[bet_type] += 1

    max_car = max(top3_count.keys(), default=9)
    car_win_prior = {}
    car_top3_prior = {}
    car_pos_prior = {1: {}, 2: {}, 3: {}}
    for car in range(1, max_car + 1):
        car_win_prior[car] = (win_count[car] + 5.0 / max_car) / (n_races + 5.0) if n_races else 1.0 / max_car
        car_top3_prior[car] = (top3_count[car] + 15.0 / max_car) / (n_races + 5.0) if n_races else 3.0 / max_car
        for pos_idx in (1, 2, 3):
            car_pos_prior[pos_idx][car] = (
                (pos_count[pos_idx][car] + 5.0 / max_car) / (n_races + 5.0)
                if n_races else 1.0 / max_car
            )
    mean_payout = {
        bt: (payout_sum[bt] / payout_cnt[bt] if payout_cnt[bt] else 0.0)
        for bt in BET_TYPES
    }
    return {
        "author_weight": author_weight,
        "car_win_prior": car_win_prior,
        "car_top3_prior": car_top3_prior,
        "car_pos_prior": car_pos_prior,
        "mean_payout": mean_payout,
        "races": n_races,
        "v1_stats": v1,
    }


# ---------------------------------------------------------------------------
# player_stats の整形
# ---------------------------------------------------------------------------
def players_to_by_race(player_records):
    """player_stats の record リスト -> {レースID: [ {車, 競走得点, 勝率, ...} ]}"""
    by_race = defaultdict(list)
    for rec in player_records or []:
        car = _num(rec.get("車"))
        if car is None:
            continue
        by_race[str(rec.get("レースID", ""))].append({
            "車": int(car),
            "選手名": rec.get("選手名", ""),
            "競走得点": _num(rec.get("競走得点")),
            "勝率": _rate(rec.get("勝率")),
            "2連対率": _rate(rec.get("2連対率")),
            "3連対率": _rate(rec.get("3連対率")),
            "AI": str(rec.get("AI", "-")).strip(),
            "脚質": rec.get("脚質", ""),
        })
    return dict(by_race)


def frames_to_players(df_players):
    records = df_players.to_dict("records") if df_players is not None else []
    return players_to_by_race(records)


# ---------------------------------------------------------------------------
# 1着確率モデル
# ---------------------------------------------------------------------------
def base_strengths(players, rating_beta=0.10, winrate_beta=2.0, ai_beta=0.35):
    """player_stats から各車の基礎強さ (正の値) を作る。

    - 競走得点: レース内平均からの差 x rating_beta を exp に入れる
      (得点差10点 -> e^1.0 ≒ 2.7倍の強さ差)
    - 勝率: レース内平均との差 x winrate_beta を線形に加える
    - AI印: スコア (0..1) の平均 (0.5) からの差 x ai_beta
    情報が無い車は 1.0 (中立)。
    """
    if not players:
        return {}
    ratings = [p["競走得点"] for p in players if p["競走得点"] is not None]
    mean_rating = sum(ratings) / len(ratings) if ratings else 0.0
    winrates = [p["勝率"] for p in players]
    mean_wr = sum(winrates) / len(winrates) if winrates else 0.0
    strengths = {}
    for p in players:
        s = 1.0
        if p["競走得点"] is not None:
            s *= math.exp(rating_beta * (p["競走得点"] - mean_rating))
        s *= max(0.2, 1.0 + winrate_beta * (p["勝率"] - mean_wr))
        ai_score = AI_MARK_SCORE.get(p["AI"], 0.5)
        s *= math.exp(ai_beta * (ai_score - 0.5) * 2.0)
        strengths[p["車"]] = s
    return strengths


def pro_support(today_preds_for_race, author_weight):
    """そのレースのPRO買い目から車ごとの支持スコアを作る。

    today_preds_for_race: [(予想家, 買い目原文)] — expand は ev_engine を流用し、
    順序あり買い目 (3連単/2車単) は着順位置で、順不同は均等にクレジットする。
    戻り値: ({車: スコア}, {買い目キー: (支持合計, [予想家])}, {着順: {車: スコア}})
    """
    car_score = defaultdict(float)
    pos_score = {1: defaultdict(float), 2: defaultdict(float), 3: defaultdict(float)}
    combo_support = {}
    for author, bet_str in today_preds_for_race:
        w = author_weight.get(author, 1.0)
        combos = ev_engine.expand_bets(bet_str, (None, None, None), None)
        seen = set()
        for c in combos:
            key = (c["券種"], c["買い目"])
            if key in seen:
                continue
            seen.add(key)
            entry = combo_support.setdefault(key, [0.0, []])
            entry[0] += w
            if author not in entry[1]:
                entry[1].append(author)
            cars = [int(x) for x in c["買い目"].replace("=", "-").split("-") if x]
            if c["券種"] in ("3連単", "2車単"):
                for pos, car in enumerate(cars):
                    if pos < len(POSITION_CREDIT):
                        credit = w * POSITION_CREDIT[pos] / max(1, len(combos) / 6)
                        car_score[car] += credit
                        pos_score[pos + 1][car] += credit
            else:
                for car in cars:
                    car_score[car] += w * 0.25 / max(1, len(combos) / 6)
                    for pos_idx in (1, 2, 3):
                        pos_score[pos_idx][car] += w * 0.1 / max(1, len(combos) / 6)
    return dict(car_score), combo_support, {k: dict(v) for k, v in pos_score.items()}


def win_probabilities(players, pro_car_score, car_win_prior,
                      pro_alpha=0.8, prior_alpha=0.5):
    """基礎強さ x 車番事前分布 x PRO支持 -> 1着確率 (合計1)。"""
    strengths = base_strengths(players)
    cars = sorted(strengths.keys()) if strengths else sorted(set(pro_car_score) | set(car_win_prior))
    if not cars:
        return {}
    max_support = max(pro_car_score.values(), default=0.0)
    raw = {}
    for car in cars:
        s = strengths.get(car, 1.0)
        prior = car_win_prior.get(car, 1.0 / len(cars))
        avg_prior = sum(car_win_prior.get(c, 1.0 / len(cars)) for c in cars) / len(cars)
        if avg_prior > 0 and prior > 0:
            s *= (prior / avg_prior) ** prior_alpha
        if max_support > 0:
            s *= 1.0 + pro_alpha * (pro_car_score.get(car, 0.0) / max_support)
        raw[car] = s
    total = sum(raw.values())
    return {car: raw[car] / total for car in cars} if total > 0 else {car: 1.0 / len(cars) for car in cars}


def position_probabilities(players, pro_pos_score, car_pos_prior,
                           pro_alpha=0.8, prior_alpha=0.5):
    """着順別 (1着/2着/3着) の確率を返す。{1: {車: p}, 2: {...}, 3: {...}}

    基礎強さは着順共通だが、車番の着順別事前分布 (例: 特定の車番が2着に
    来やすい) と、PROの着順別支持で補正する。ライン構造による
    「勝った車の後ろに付きやすい車」を確率に反映するための拡張Harville。
    """
    strengths = base_strengths(players)
    all_cars = set(strengths) | set(car_pos_prior.get(1, {}))
    for scores in (pro_pos_score or {}).values():
        all_cars |= set(scores)
    cars = sorted(all_cars)
    if not cars:
        return {1: {}, 2: {}, 3: {}}
    out = {}
    for pos_idx in (1, 2, 3):
        prior_map = car_pos_prior.get(pos_idx, {})
        scores = (pro_pos_score or {}).get(pos_idx, {})
        max_support = max(scores.values(), default=0.0)
        avg_prior = (sum(prior_map.get(c, 1.0 / len(cars)) for c in cars) / len(cars)) if cars else 0.0
        raw = {}
        for car in cars:
            s = strengths.get(car, 1.0)
            prior = prior_map.get(car, 1.0 / len(cars))
            if avg_prior > 0 and prior > 0:
                s *= (prior / avg_prior) ** prior_alpha
            if max_support > 0:
                s *= 1.0 + pro_alpha * (scores.get(car, 0.0) / max_support)
            raw[car] = s
        total = sum(raw.values())
        out[pos_idx] = ({car: raw[car] / total for car in cars} if total > 0
                        else {car: 1.0 / len(cars) for car in cars})
    return out


# ---------------------------------------------------------------------------
# Harville式
# ---------------------------------------------------------------------------
def _prob_ordered(win_p, combo, pos_p=None):
    """Harville式。pos_p ({着順: {車: p}}) があれば着順別確率で計算する。"""
    p = 1.0
    used_by_pos = {}
    for idx, car in enumerate(combo, start=1):
        probs = (pos_p or {}).get(idx, win_p)
        used = sum(probs.get(c, 0.0) for c in used_by_pos)
        denom = 1.0 - used
        if denom <= 0:
            return 0.0
        p *= probs.get(car, 0.0) / denom
        used_by_pos[car] = True
    return p


def combo_probability(win_p, bet_type, cars, pos_p=None):
    cars = tuple(cars)
    if bet_type in ("3連単", "2車単"):
        return _prob_ordered(win_p, cars, pos_p)
    if bet_type in ("3連複", "2車複"):
        return sum(_prob_ordered(win_p, perm, pos_p) for perm in itertools.permutations(cars))
    if bet_type == "ワイド":
        total = 0.0
        others = [c for c in win_p if c not in cars]
        for perm in itertools.permutations(cars):
            total += _prob_ordered(win_p, perm, pos_p)
            for third in others:
                total += _prob_ordered(win_p, (third, perm[0], perm[1]), pos_p)
                total += _prob_ordered(win_p, (perm[0], third, perm[1]), pos_p)
        return total
    return 0.0


def _combo_str(bet_type, cars):
    if bet_type in ("3連単", "2車単"):
        return "-".join(str(c) for c in cars)
    return "=".join(str(c) for c in sorted(cars))


def _all_combos(bet_type, cars):
    if bet_type == "3連単":
        return itertools.permutations(cars, 3)
    if bet_type == "3連複":
        return itertools.combinations(sorted(cars), 3)
    if bet_type == "2車単":
        return itertools.permutations(cars, 2)
    if bet_type in ("2車複", "ワイド"):
        return itertools.combinations(sorted(cars), 2)
    return []


# ---------------------------------------------------------------------------
# スコアリング本体 (v1 score_upcoming と同形式の DataFrame を返す)
# ---------------------------------------------------------------------------
def score_upcoming_v2(memos, results_by_id, players_by_race, stats_v2,
                      midnight_only=False, bet_types=None, odds_map=None,
                      from_date=None, top_per_race_type=15, require_pro=False):
    odds_map = odds_map or {}
    bet_types = bet_types or BET_TYPES
    author_weight = stats_v2["author_weight"]
    car_prior = stats_v2["car_win_prior"]
    mean_payout = stats_v2["mean_payout"]

    # レースごとに、そのレースのPRO予想と選手データを集める
    preds_by_race = defaultdict(list)
    race_time = {}
    for pred in ev_engine.iter_predictions(memos, results_by_id):
        if pred["has_result"]:
            continue
        if from_date and pred["レースID"][:8] < ev_engine._race_date_threshold(from_date):
            continue
        if midnight_only and not ev_engine.is_midnight_time(pred["発走時間"]):
            continue
        preds_by_race[pred["レースID"]].append((pred["予想家"], pred["買い目原文"]))
        race_time[pred["レースID"]] = pred["発走時間"]

    # PRO予想が無いレースでも player_stats があれば対象にできる (require_pro=False)
    if not require_pro:
        for race_id in players_by_race:
            if race_id in results_by_id:
                rec = results_by_id[race_id]
                if all(pd.notnull(rec.get(c)) for c in ("1着車番", "2着車番", "3着車番")):
                    continue
            if from_date and race_id[:8] < ev_engine._race_date_threshold(from_date):
                continue
            preds_by_race.setdefault(race_id, [])
            race_time.setdefault(race_id, "-")

    rows = []
    for race_id, preds in preds_by_race.items():
        players = players_by_race.get(race_id, [])
        if not players and not preds:
            continue
        pro_car_score, combo_support, pro_pos_score = pro_support(preds, author_weight)
        win_p = win_probabilities(players, pro_car_score, car_prior)
        if not win_p:
            continue
        pos_p = position_probabilities(
            players, pro_pos_score, stats_v2.get("car_pos_prior", {1: car_prior, 2: car_prior, 3: car_prior})
        )
        cars = sorted(win_p.keys())
        venue = ev_engine._venue_of(race_id)
        for bet_type in bet_types:
            cand = []
            for cars_combo in _all_combos(bet_type, cars):
                combo = _combo_str(bet_type, cars_combo)
                p = combo_probability(win_p, bet_type, cars_combo, pos_p)
                if p <= 0:
                    continue
                odds = odds_map.get((race_id, bet_type, combo))
                if odds:
                    ev = p * float(odds)
                    basis = "確率モデルx現在オッズ"
                else:
                    mp = mean_payout.get(bet_type, 0.0)
                    ev = p * mp / 100.0 if mp > 0 else 0.0
                    basis = "確率モデルx的中時平均払戻"
                support, authors = combo_support.get((bet_type, combo), [0.0, []])
                cand.append({
                    "レースID": race_id,
                    "会場": venue,
                    "発走時間": race_time.get(race_id, "-"),
                    "券種": bet_type,
                    "買い目": combo,
                    "予想家": "、".join(authors),
                    "予想家人数": len(authors),
                    "推定EV": round(ev, 3),
                    "推定回収率%": round(ev * 100, 1),
                    "推定的中率%": round(p * 100, 2),
                    "根拠": basis + ("+PRO支持" if support > 0 else ""),
                    "根拠内訳": f"PRO支持:{support:.2f}" if support > 0 else "選手データのみ",
                    "根拠サンプル点数": int(round(support * 10)),
                    "オッズ": odds if odds else "",
                    "_p": p,
                })
            cand.sort(key=lambda r: r["推定EV"], reverse=True)
            rows.extend(cand[:top_per_race_type])

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop(columns=["_p"]).sort_values(
            by=["推定EV", "推定的中率%"], ascending=False
        ).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# セルフテスト
#   実行: python3 ev_v2.py --selftest
# ---------------------------------------------------------------------------
def _selftest():
    import json
    import random
    import sys  # noqa: F401

    rng = random.Random(11)

    def payout_json(a, b, c):
        pairs = sorted([tuple(sorted(x)) for x in [(a, b), (a, c), (b, c)]])
        return json.dumps([
            {"賭け式": "3連単", "組番": f"{a}-{b}-{c}", "払戻金": "2500円"},
            {"賭け式": "3連複", "組番": f"{min(a,b,c)}={sorted([a,b,c])[1]}={max(a,b,c)}", "払戻金": "800円"},
            {"賭け式": "2車単", "組番": f"{a}-{b}", "払戻金": "600円"},
        ] + [{"賭け式": "ワイド", "組番": f"{x}={y}", "払戻金": "250円"} for (x, y) in pairs],
            ensure_ascii=False)

    memos, results_by_id, players = [], {}, []
    # 過去120レース: 車1が強く (得点も高い)、strong は 1-2-3、weak は 4-5-6
    for i in range(120):
        race_id = f"20260101_テスト_{i:03d}"
        u = rng.random()
        result = (1, 2, 3) if u < 0.5 else (4, 5, 6) if u < 0.6 else tuple(rng.sample(range(1, 8), 3))
        if result in [(1, 2, 3), (4, 5, 6)] and u >= 0.6:
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
        for car in range(1, 8):
            players.append({
                "レースID": race_id, "車": car, "選手名": f"選手{car}",
                "AI": "◎" if car == 1 else "-",
                "競走得点": 100 - car * 2, "勝率": max(0.02, 0.30 - car * 0.04),
                "2連対率": 0.4, "3連対率": 0.5, "脚質": "両",
            })

    # 未来レース
    future_id = "20260102_テスト_000"
    memos.append({
        "レースID": future_id, "発走時間": "21:00",
        "構造化データJSON": json.dumps([
            {"予想家": "strong", "買い目": "1-2-3", "コメント": ""},
            {"予想家": "weak", "買い目": "4-5-6", "コメント": ""},
        ], ensure_ascii=False),
    })
    for car in range(1, 8):
        players.append({
            "レースID": future_id, "車": car, "選手名": f"選手{car}",
            "AI": "◎" if car == 1 else "-",
            "競走得点": 100 - car * 2, "勝率": max(0.02, 0.30 - car * 0.04),
            "2連対率": 0.4, "3連対率": 0.5, "脚質": "両",
        })

    stats = build_history_v2(memos, results_by_id)
    print(f"author_weight: strong={stats['author_weight'].get('strong')}, weak={stats['author_weight'].get('weak')}")
    assert stats["author_weight"]["strong"] > stats["author_weight"]["weak"]
    assert stats["car_win_prior"][1] > stats["car_win_prior"][7]

    by_race = players_to_by_race(players)
    win_p = win_probabilities(by_race[future_id], {}, stats["car_win_prior"])
    assert abs(sum(win_p.values()) - 1.0) < 1e-9
    assert win_p[1] == max(win_p.values()), "得点トップの1番車が1着確率トップでない"
    print(f"win_p car1={win_p[1]:.3f} car7={win_p[7]:.3f}")

    # Harville: 3連単の全確率 = 1
    tot = sum(combo_probability(win_p, "3連単", perm) for perm in itertools.permutations(range(1, 8), 3))
    assert abs(tot - 1.0) < 1e-6

    ranked = score_upcoming_v2(memos, results_by_id, by_race, stats, from_date="20260102")
    assert not ranked.empty
    top_rows = ranked[ranked["レースID"] == future_id]
    assert not top_rows.empty
    top_2t = top_rows[top_rows["券種"] == "2車単"].iloc[0]
    print(f"top 2車単: {top_2t['買い目']} EV={top_2t['推定EV']} 的中率={top_2t['推定的中率%']}%")
    assert top_2t["買い目"].startswith("1-"), "2車単のトップが1番車軸でない"
    print("V2 SELFTEST PASSED")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
