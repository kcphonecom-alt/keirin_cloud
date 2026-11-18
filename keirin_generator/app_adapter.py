"""Streamlit アプリ (app_cloud.py) 用アダプタ。

keirin_generator の CLI (gen.py) は data/ 配下の JSON ダンプを読むが、
Streamlit Cloud 上ではローカルファイルに依存できない。そこでこのアダプタが、
アプリが Supabase から読んだ DataFrame (race_results / race_memos /
external_predictions / player_stats) から、inputs.build_race_inputs と
まったく同じ「1レース = 1 dict」(RaceInput) を組み立てる。

- 行の変換・組み立てロジックは inputs.py の各ローダと揃えてある
  (parse_race_id / _parse_formation / _yen は inputs.py のものを再利用)。
- 記者印 (journalist) は DB に無いため、アプリ経由では通常 None になる。
  検証用に、inputs.load_journalist_marks() 形式の dict を渡すこともできる。
- engine.py は内部で `import rules` するため、兄弟モジュールは
  sys.modules に本来の名前で登録してから読み込む。
"""
import importlib.util
import json
import math
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
FORWARD_DIR = os.path.join(HERE, "forward")
RULES_PATH = os.path.join(HERE, "rules_sample.json")
REPORT_PATH = os.path.join(HERE, "report_demo.json")


# ---------------------------------------------------------------- モジュール読込
def _load_sibling(name):
    mod = sys.modules.get(name)
    if mod is not None and getattr(mod, "_kg_sibling", False):
        return mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    mod._kg_sibling = True
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_modules():
    """(inputs, rules, engine) を読み込んで返す。"""
    rules_mod = _load_sibling("rules")
    inputs_mod = _load_sibling("inputs")
    engine_mod = _load_sibling("engine")
    return inputs_mod, rules_mod, engine_mod


# ---------------------------------------------------------------- 静的ファイル
def load_rules(rules_mod):
    return rules_mod.load_rules(RULES_PATH)


def load_demo_report():
    """report_demo.json (CLI の demo 実演結果)。無ければ None。"""
    if not os.path.exists(REPORT_PATH):
        return None
    with open(REPORT_PATH, encoding="utf-8") as f:
        return json.load(f)


def list_forward_dates():
    if not os.path.isdir(FORWARD_DIR):
        return []
    return sorted(fn[:-5] for fn in os.listdir(FORWARD_DIR)
                  if fn.endswith(".json"))


def load_forward(date_str):
    """forward/YYYY-MM-DD.json があれば dict、無ければ None。"""
    path = os.path.join(FORWARD_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- DataFrame -> 行
def _clean_value(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy スカラー
        try:
            return v.item()
        except Exception:
            return v
    return v


def rows_from_frame(df):
    """DataFrame -> list[dict]。NaN は None に、日付は ISO 文字列にそろえる
    (inputs.py が JSON ダンプ前提の形で書かれているため)。"""
    if df is None or len(df) == 0:
        return []
    return [{k: _clean_value(v) for k, v in rec.items()}
            for rec in df.to_dict("records")]


def _prefilter_frame(df, only_date, window_days=7):
    """only_date のレースが入り得る行だけに絞る (レースIDの開催初日は
    レース日の最大 window_days 日前まで)。変換コスト削減用。"""
    if df is None or len(df) == 0 or not only_date or "レースID" not in df.columns:
        return df
    try:
        target = date.fromisoformat(only_date)
    except ValueError:
        return df
    lo = (target - timedelta(days=window_days)).strftime("%Y%m%d")
    hi = target.strftime("%Y%m%d")
    prefix = df["レースID"].astype(str).str.slice(0, 8)
    return df[(prefix >= lo) & (prefix <= hi)]


# ---------------------------------------------------------------- 行 -> RaceInput
def _results_from_rows(rows, inputs_mod, only_date=None):
    """inputs.load_results の行版。"""
    by_id, by_key = {}, {}
    for r in rows:
        rid = r.get("レースID")
        parsed = inputs_mod.parse_race_id(rid)
        if not parsed:
            continue
        if only_date and parsed[0] != only_date:
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
                amt = inputs_mod._yen(item.get("払戻金"))
                if item.get("賭け式") and item.get("組番") and amt:
                    payouts[(item["賭け式"], str(item["組番"]))] = amt
        by_id[rid] = {"finish": [int(x) for x in finish], "payouts": payouts,
                      "canceled": False}
    return by_id, by_key


def _pro_from_rows(rows, inputs_mod, only_date=None):
    """inputs.load_pro_memos の行版。"""
    out = {}
    for r in rows:
        if only_date:
            parsed = inputs_mod.parse_race_id(r.get("レースID"))
            if not parsed or parsed[0] != only_date:
                continue
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
            cols = inputs_mod._parse_formation(bet_str)
            preds.append({
                "author": p.get("予想家", "不明"),
                "bet_str": bet_str,
                "columns": cols,
                "axis": cols[0] if cols else [],
            })
        if preds:
            out[r["レースID"]] = preds
    return out


def _kc_from_rows(rows, only_date=None):
    """inputs.load_keirincraft の行版。"""
    out = {}
    for r in rows:
        if r.get("ソース") != "keirincraft":
            continue
        if only_date and str(r.get("レース日")) != only_date:
            continue
        pj = r.get("予想JSON") or {}
        if isinstance(pj, str):
            try:
                pj = json.loads(pj)
            except Exception:
                pj = {}
        top3 = pj.get("top3") if isinstance(pj, dict) else None
        if not top3:
            continue
        key = (str(r["レース日"]), r["会場"], int(r["レース番号"]))
        out.setdefault(key, {})[r["予想種別"]] = [int(x) for x in top3]
    return out


def _stats_from_rows(rows, inputs_mod, only_date=None):
    """inputs.load_player_stats の行版。"""
    out = {}
    for r in rows:
        if only_date:
            parsed = inputs_mod.parse_race_id(r.get("レースID") or r.get("race_id"))
            if not parsed or parsed[0] != only_date:
                continue
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


def build_race_inputs_from_frames(df_results, df_memos, df_kc, df_stats,
                                  journalists=None, only_date=None):
    """DataFrame 群から RaceInput dict {(date, venue, race_no): race} を作る。
    組み立て順は inputs.build_race_inputs と同じ。"""
    inputs_mod, _, _ = load_modules()
    results_by_id, results_by_key = _results_from_rows(
        rows_from_frame(_prefilter_frame(df_results, only_date)),
        inputs_mod, only_date)
    pro = _pro_from_rows(
        rows_from_frame(_prefilter_frame(df_memos, only_date)),
        inputs_mod, only_date)
    kc = _kc_from_rows(rows_from_frame(df_kc), only_date)
    stats = _stats_from_rows(
        rows_from_frame(_prefilter_frame(df_stats, only_date)),
        inputs_mod, only_date)
    journalists = journalists or {}

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
        key = inputs_mod.parse_race_id(rid)
        if key:
            ensure(key, rid)["sources"]["pro"] = preds
    for key, kinds in kc.items():
        ensure(key, results_by_key.get(key))["sources"]["keirincraft"] = kinds
    for key, j in journalists.items():
        if only_date and key[0] != only_date:
            continue
        r = ensure(key, results_by_key.get(key))
        r["sources"]["journalist"] = j
        if j.get("riders"):
            r["sources"]["riders"] = j["riders"]
    for rid, riders in stats.items():
        key = inputs_mod.parse_race_id(rid)
        if key:
            ensure(key, rid)["sources"]["riders"] = riders
    for rid, res in results_by_id.items():
        key = inputs_mod.parse_race_id(rid)
        if key and res is not None:
            ensure(key, rid)["result"] = res
    return races


def generate_for_date(races, rule_list, date_str):
    """指定日の買い目を生成し、結果があれば精算まで済ませて返す。"""
    _, _, engine_mod = load_modules()
    bets = engine_mod.generate(races, rule_list, only_date=date_str)
    return engine_mod.settle(bets, races)


def summarize_bets(bets):
    """買い目リストをルール別に集計する。
    回収率は精算済み分のみで計算する (未精算を投資に含めない)。"""
    per = {}
    for b in bets:
        s = per.setdefault(b["rule_id"], {
            "points": 0, "settled": 0, "hits": 0,
            "invested": 0, "returned": 0, "races": set()})
        stake = int(b.get("stake", 100))
        s["points"] += 1
        s["races"].add((b["date"], b["venue"], b["race_no"]))
        if b.get("settled"):
            s["settled"] += 1
            s["invested"] += stake
            if b.get("hit"):
                s["hits"] += 1
                s["returned"] += int(b.get("payout", 0)) * stake // 100
    for s in per.values():
        s["roi_pct"] = (round(s["returned"] / s["invested"] * 100, 1)
                        if s["invested"] else None)
        s["races"] = len(s["races"])
    return per
