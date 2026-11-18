"""ルール定義と解決エンジン。

ルールは JSON で差し替え可能。KB1013 風の「ソース横断の印の組み合わせ →
買い目」を表現する。

## セレクタ (ref) — 選手(車番)の集合を指す文字列
- "journalist.◎" / "journalist.○" / "journalist.▲" / "journalist.△" /
  "journalist.×" / "journalist.注"   記者の印
- "kc_ai.1" / "kc_ai.2" / "kc_ai.3" / "kc_ai.top3"        KEIRINCRAFT AI (朝版)
- "kc_stats.1" ... "kc_stats.top3"                        KEIRINCRAFT 統計 (朝版)
- "kc_ai_narabi.1" / "kc_stats_narabi.1" など              並び後版
- "pro:<予想家名>.axis"    その予想家のフォーメーション1列目 (軸候補集合)
- "pro_consensus.axis"     PRO予想家の1列目に最も多く入った選手 (同数なら全員)
- "score.1" / "score.2" / "score.3"   競走得点の順位 (riders 情報から)

## 条件 (when) — 全て満たしたときだけ発動。各条件は dict:
- {"type": "same_rider", "a": ref, "b": ref}        a と b に共通の選手がいる
- {"type": "in_set", "a": ref, "b": ref}            a の選手が全て b に含まれる
- {"type": "journalist_is", "name": "真島秀昭"}     担当記者がその人 (部分一致)
- {"type": "score_rank_of", "ref": ref, "rank": 1}  ref の選手の得点順位が rank
- {"type": "score_gap", "min": 3.0}                 得点1位と2位の差が min 以上
- {"type": "pro_consensus_count", "min": 3}         軸一致の予想家数が min 以上
- {"type": "overlap_count", "a": ref, "b": ref, "min": 2}  共通選手数が min 以上

## 買い目 (bet) — dict:
- {"type": "2車単", "from": ref, "to": ref}              from→to (from≠to)
- {"type": "ワイド", "a": ref, "b": ref}                  a=b の組 (a≠b)
- {"type": "3連複", "box": [ref, ref, ref]}               3選手のBOX (1点)
- {"type": "3連単", "order": [ref, ref, ref]}             順序指定 (各ref単独選手のとき)
- {"type": "2車単フォーメーション", "axis": ref, "partners": ref}
                                                         axis の各選手 → partners の各選手

ルール全体:
{"id": "一意なID", "name": "表示名", "description": "意図の説明",
 "when": [条件...], "bet": {...}, "stake": 100}
"""
import itertools
import json

KC_KINDS = {
    "kc_ai": "ai_asa", "kc_stats": "stats_asa",
    "kc_ai_narabi": "ai_narabi", "kc_stats_narabi": "stats_narabi",
}


def resolve(ref, race):
    """セレクタを車番の集合 (set[int]) に解決する。解決できなければ空集合。"""
    src = race["sources"]
    if ref.startswith("journalist."):
        j = src.get("journalist")
        if not j:
            return set()
        return set(j["marks"].get(ref.split(".", 1)[1], []))
    if ref.startswith("kc_"):
        kind, _, pos = ref.rpartition(".")
        top3 = (src.get("keirincraft") or {}).get(KC_KINDS.get(kind, ""))
        if not top3:
            return set()
        if pos == "top3":
            return set(top3)
        if pos.isdigit() and 1 <= int(pos) <= len(top3):
            return {top3[int(pos) - 1]}
        return set()
    if ref == "pro_consensus.axis":
        return set(_pro_consensus(race)[0])
    if ref.startswith("pro:") and ref.endswith(".axis"):
        name = ref[4:-5]
        for p in src.get("pro", []):
            if p["author"] == name:
                return set(p["axis"])
        return set()
    if ref.startswith("score."):
        pos = ref.split(".", 1)[1]
        ranked = _score_ranking(race)
        if pos.isdigit() and 1 <= int(pos) <= len(ranked):
            return {ranked[int(pos) - 1]}
        return set()
    return set()


def _score_ranking(race):
    """競走得点の高い順の車番リスト (得点がある選手のみ)"""
    riders = race["sources"].get("riders") or {}
    scored = [(info.get("score"), car) for car, info in riders.items()
              if info.get("score") is not None]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [car for _, car in scored]


def _pro_consensus(race):
    """(最多得票の軸選手リスト, その得票数)"""
    from collections import Counter
    cnt = Counter()
    for p in race["sources"].get("pro", []):
        for car in set(p["axis"]):
            cnt[car] += 1
    if not cnt:
        return [], 0
    top = max(cnt.values())
    return sorted(c for c, n in cnt.items() if n == top), top


def check_condition(cond, race):
    t = cond["type"]
    if t == "same_rider":
        return bool(resolve(cond["a"], race) & resolve(cond["b"], race))
    if t == "in_set":
        a = resolve(cond["a"], race)
        return bool(a) and a <= resolve(cond["b"], race)
    if t == "journalist_is":
        j = race["sources"].get("journalist")
        return bool(j) and cond["name"] in (j.get("name") or "")
    if t == "score_rank_of":
        target = resolve(cond["ref"], race)
        ranked = _score_ranking(race)
        return any(car in ranked and ranked.index(car) + 1 == cond["rank"]
                   for car in target)
    if t == "score_gap":
        riders = race["sources"].get("riders") or {}
        scores = sorted((info["score"] for info in riders.values()
                         if info.get("score") is not None), reverse=True)
        return len(scores) >= 2 and (scores[0] - scores[1]) >= cond["min"]
    if t == "pro_consensus_count":
        return _pro_consensus(race)[1] >= cond["min"]
    if t == "overlap_count":
        return len(resolve(cond["a"], race) & resolve(cond["b"], race)) >= cond["min"]
    raise ValueError(f"unknown condition type: {t}")


def build_bets(rule, race):
    """ルールを1レースに適用し、買い目 [(券種, 組番文字列)] を返す。

    組番文字列は race_results の払戻金JSON の表記に合わせる:
    2車単 "4-3" / ワイド "2=3" (昇順) / 3連複 "2=3=4" (昇順) / 3連単 "4-3-2"
    """
    for cond in rule.get("when", []):
        if not check_condition(cond, race):
            return []
    bet = rule["bet"]
    t = bet["type"]
    bets = []
    if t == "2車単":
        for a in sorted(resolve(bet["from"], race)):
            for b in sorted(resolve(bet["to"], race)):
                if a != b:
                    bets.append(("2車単", f"{a}-{b}"))
    elif t == "ワイド":
        for a in sorted(resolve(bet["a"], race)):
            for b in sorted(resolve(bet["b"], race)):
                if a != b:
                    lo, hi = min(a, b), max(a, b)
                    bets.append(("ワイド", f"{lo}={hi}"))
    elif t == "3連複":
        pool = set()
        for ref in bet["box"]:
            pool |= resolve(ref, race)
        for combo in itertools.combinations(sorted(pool), 3):
            bets.append(("3連複", "=".join(str(c) for c in combo)))
    elif t == "3連単":
        cols = [sorted(resolve(ref, race)) for ref in bet["order"]]
        for combo in itertools.product(*cols):
            if len(set(combo)) == 3:
                bets.append(("3連単", "-".join(str(c) for c in combo)))
    elif t == "2車単フォーメーション":
        for a in sorted(resolve(bet["axis"], race)):
            for b in sorted(resolve(bet["partners"], race)):
                if a != b:
                    bets.append(("2車単", f"{a}-{b}"))
    else:
        raise ValueError(f"unknown bet type: {t}")
    return sorted(set(bets))


def load_rules(path):
    with open(path, encoding="utf-8") as f:
        rules = json.load(f)
    if isinstance(rules, dict):
        rules = rules["rules"]
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids)), "rule id が重複しています"
    return rules
