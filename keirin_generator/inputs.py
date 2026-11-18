"""入力の統一層。

各データソースを「1レース = 1つの dict」に統一する。

RaceInput の形:
{
  "race_id": "20261113_松戸_1R_d1" or None,
  "date": "2026-11-13",            # レース日 (開催初日 + 日次 - 1)
  "venue": "松戸",
  "race_no": 1,
  "start_time": "11:13" or None,
  "sources": {
    "pro": [ {"author": "ふるだび", "bet_str": "7-35-35241",
              "axis": [7], "columns": [[7], [3,5], [3,5,2,4,1]]}, ... ],
    "keirincraft": {"ai_asa": [5,3,4], "stats_asa": [...],
                    "ai_narabi": [...] or None, "stats_narabi": [...] or None},
    "journalist": {"name": "真島秀昭",
                   "marks": {"◎": [1], "○": [4], "▲": [7], ...},
                   "riders": {1: {"mark": "◎", "score": 90.1, "style": "逃",
                                  "grade": "S1", "name": "…"}, ...}} or None,
    "riders": {1: {"score": 90.1, "style": "逃", "grade": "S1", "name": "…"}, ...}
              # player_stats 由来 (無ければ journalist.riders で代替)
  },
  "result": {"finish": [4,3,2], "payouts": {("2車単","4-3"): 2810, ...}} or None
}

データの出どころ:
- race_results / race_memos / external_predictions / player_stats:
  data/ 配下の Supabase ダンプ (supabase_fetch.py で取得した JSON)
- 記者印: ~/workspace/majima/watch/*.json (真島・八角ウォッチ分) と
  ~/workspace/majima/today_parsed_*.json (Kドリームス全レース解析キャッシュ、
  parse_lib.py 形式)。全レース分の記者印を DB 化する仕組みはまだ無いので、
  「あれば取り込む」インターフェースに留める。
"""
import glob
import json
import os
import re
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
MAJIMA_DIR = os.path.expanduser("~/workspace/majima")

_RACE_ID_RE = re.compile(r"(\d{8})_(.+)_(\d+)R_d(\d+)")


def parse_race_id(race_id):
    """'20261113_松戸_1R_d1' -> (date, venue, race_no) / 失敗時 None"""
    m = _RACE_ID_RE.match(str(race_id or ""))
    if not m:
        return None
    start = date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8]))
    return (start + timedelta(days=int(m.group(4)) - 1)).isoformat(), m.group(2), int(m.group(3))


def _yen(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- results
def load_results(path=None):
    """race_results -> {race_id: result dict}, {(date, venue, race_no): race_id}"""
    rows = _load_json(path or os.path.join(DATA_DIR, "race_results.json"))
    by_id, by_key = {}, {}
    for r in rows:
        rid = r.get("レースID")
        parsed = parse_race_id(rid)
        if not parsed:
            continue
        by_key[parsed] = rid
        if r.get("is_canceled"):
            by_id[rid] = {"finish": None, "payouts": {}, "canceled": True}
            continue
        finish = [r.get("1着車番"), r.get("2着車番"), r.get("3着車番")]
        if any(x is None for x in finish):
            by_id[rid] = None
            continue
        payouts = {}
        raw = r.get("払戻金JSON")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None
        if isinstance(raw, list):
            for item in raw:
                amt = _yen(item.get("払戻金"))
                if item.get("賭け式") and item.get("組番") and amt:
                    payouts[(item["賭け式"], str(item["組番"]))] = amt
        by_id[rid] = {"finish": [int(x) for x in finish], "payouts": payouts,
                      "canceled": False}
    return by_id, by_key


# ---------------------------------------------------------------- PRO予想
def _parse_formation(bet_str):
    """'37-37-245' / '1=4-236' -> [[3,7],[3,7],[2,4,5]] のような列リスト"""
    if not bet_str:
        return []
    cols = []
    for part in re.split(r"[-ー]", str(bet_str)):
        cars = [int(c) for c in re.findall(r"\d", part)]
        if cars:
            cols.append(cars)
    return cols


def load_pro_memos(path=None):
    """race_memos -> {race_id: [pro prediction, ...]} (構造化JSONがある分のみ)"""
    rows = _load_json(path or os.path.join(DATA_DIR, "race_memos.json"))
    out = {}
    for r in rows:
        raw = r.get("構造化データJSON")
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                continue
        if not isinstance(raw, list):
            continue
        preds = []
        for p in raw:
            if not isinstance(p, dict):
                continue
            bet_str = p.get("買い目") or p.get("買い目を抽出する") or ""
            cols = _parse_formation(bet_str)
            preds.append({
                "author": p.get("予想家", "不明"),
                "bet_str": bet_str,
                "columns": cols,
                "axis": cols[0] if cols else [],
            })
        if preds:
            out[r["レースID"]] = preds
    return out


# ---------------------------------------------------------------- KEIRINCRAFT
def load_keirincraft(path=None):
    """external_predictions -> {(date, venue, race_no): {kind: top3}}"""
    rows = _load_json(path or os.path.join(DATA_DIR, "external_predictions.json"))
    out = {}
    for r in rows:
        if r.get("ソース") != "keirincraft":
            continue
        pj = r.get("予想JSON") or {}
        top3 = pj.get("top3") if isinstance(pj, dict) else None
        if not top3:
            continue
        key = (r["レース日"], r["会場"], int(r["レース番号"]))
        out.setdefault(key, {})[r["予想種別"]] = [int(x) for x in top3]
    return out


# ---------------------------------------------------------------- 記者印
def _marks_from_riders(riders):
    marks = {}
    for car, info in riders.items():
        mk = info.get("mark")
        if mk:
            marks.setdefault(mk, []).append(int(car))
    return marks


def load_journalist_marks(majima_dir=None):
    """記者印 -> {(date, venue, race_no): journalist dict}

    ソース1: majima/watch/*.json (真島・八角のウォッチ記録。印は ◎○▲ のみ)
    ソース2: majima/today_parsed_*.json (parse_lib 形式の全レース解析キャッシュ。
             記者名が取れたレースだけ取り込む)
    """
    base = majima_dir or MAJIMA_DIR
    out = {}
    # ソース2を先に入れ、ウォッチ (より確実な記録) で上書きする
    for path in sorted(glob.glob(os.path.join(base, "today_parsed_*.json"))):
        try:
            pages = _load_json(path)
        except Exception:
            continue
        for pg in pages:
            name = (pg.get("journalist") or "").strip()
            if not name.startswith("アオケイ"):
                continue
            riders = {}
            for car, info in (pg.get("riders") or {}).items():
                riders[int(car)] = {
                    "mark": info.get("mark") or "",
                    "score": info.get("score"),
                    "style": info.get("style") or "",
                    "grade": info.get("grade") or "",
                    "name": info.get("name") or "",
                }
            if not riders:
                continue
            key = (pg.get("date"), pg.get("venue"), int(pg.get("race_no") or 0))
            out[key] = {"name": name.replace("アオケイ ", "").replace("アオケイ", "").strip(),
                        "marks": _marks_from_riders(riders), "riders": riders,
                        "origin": "kdreams_parsed_cache"}
    for path in sorted(glob.glob(os.path.join(base, "watch", "*.json"))):
        try:
            w = _load_json(path)
        except Exception:
            continue
        for race in w.get("races", []):
            marks = {mk: [int(c)] for mk, c in (race.get("marks") or {}).items()}
            if not marks:
                continue
            key = (race.get("date") or w.get("date"), race.get("venue") or w.get("venue"),
                   int(race.get("race_no") or 0))
            out[key] = {"name": race.get("journalist") or w.get("journalist") or "",
                        "marks": marks, "riders": {}, "origin": "watch"}
    return out


# ---------------------------------------------------------------- player_stats
def load_player_stats(path=None):
    """player_stats -> {race_id: {car: rider info}}。ファイルが無ければ {}。

    player_stats の列は環境で変わり得るため、代表的な列名を吸収する。
    """
    p = path or os.path.join(DATA_DIR, "player_stats.json")
    if not os.path.exists(p):
        return {}
    rows = _load_json(p)
    out = {}
    for r in rows:
        rid = r.get("レースID") or r.get("race_id")
        car = r.get("車") or r.get("車番") or r.get("car")
        if rid is None or car is None:
            continue
        score = r.get("競走得点") or r.get("得点") or r.get("score")
        out.setdefault(rid, {})[int(car)] = {
            "score": float(score) if score not in (None, "") else None,
            "style": r.get("脚質") or r.get("style") or "",
            "grade": r.get("級班") or r.get("grade") or "",
            "name": r.get("選手名") or r.get("name") or "",
            "win_rate": r.get("勝率"),
            "top3_rate": r.get("3連対率"),
        }
    return out


# ---------------------------------------------------------------- 統合
def build_race_inputs(data_dir=None, majima_dir=None):
    """全ソースを統合し、{(date, venue, race_no): RaceInput} を返す。"""
    global DATA_DIR
    if data_dir:
        DATA_DIR = data_dir
    results_by_id, results_by_key = load_results()
    pro = load_pro_memos()
    kc = load_keirincraft()
    journalists = load_journalist_marks(majima_dir)
    stats = load_player_stats()

    races = {}

    def ensure(key, race_id=None):
        if key not in races:
            races[key] = {
                "race_id": race_id, "date": key[0], "venue": key[1],
                "race_no": key[2], "start_time": None,
                "sources": {"pro": [], "keirincraft": {}, "journalist": None,
                            "riders": {}},
                "result": None,
            }
        if race_id and not races[key]["race_id"]:
            races[key]["race_id"] = race_id
        return races[key]

    for rid, preds in pro.items():
        key = parse_race_id(rid)
        if key:
            ensure(key, rid)["sources"]["pro"] = preds
    for key, kinds in kc.items():
        ensure(key, results_by_key.get(key))["sources"]["keirincraft"] = kinds
    for key, j in journalists.items():
        r = ensure(key, results_by_key.get(key))
        r["sources"]["journalist"] = j
        if j.get("riders"):
            r["sources"]["riders"] = j["riders"]
    for rid, riders in stats.items():
        key = parse_race_id(rid)
        if key:
            ensure(key, rid)["sources"]["riders"] = riders
    for rid, res in results_by_id.items():
        key = parse_race_id(rid)
        if key and res is not None:
            ensure(key, rid)["result"] = res
    return races


if __name__ == "__main__":
    races = build_race_inputs()
    n_pro = sum(1 for r in races.values() if r["sources"]["pro"])
    n_kc = sum(1 for r in races.values() if r["sources"]["keirincraft"])
    n_j = sum(1 for r in races.values() if r["sources"]["journalist"])
    n_riders = sum(1 for r in races.values() if r["sources"]["riders"])
    n_res = sum(1 for r in races.values() if r["result"])
    print(f"races total: {len(races)} | PRO: {n_pro} | KC: {n_kc} | "
          f"journalist: {n_j} | riders: {n_riders} | settled: {n_res}")
