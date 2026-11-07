# -*- coding: utf-8 -*-
"""
選手コメント傾向分析モジュール。

player_stats の「コメント」（出走前に各選手へ付けられた短いコメント）と
race_results（着順・払戻金）を車番単位で照合し、

  1. コメント原文（フレーズ）ごとの成績（1着率・連対率・3連対率）
  2. コメントの分類（自力宣言／番手宣言／単騎／好調／不安 など）ごとの成績
  3. その選手を軸に「2車単 総流し」「ワイド 総流し」を買い続けた場合の
     回収率シミュレーション（実際の払戻金で計算）
  4. 分類 × 条件（脚質・会場・車番帯・得点帯・AI印）の組み合わせから
     回収率の高い「法則候補」を自動抽出

を行う。DB へは接続せず、DataFrame を受け取る純粋関数のみで構成する
（app_cloud.py の load_replica_data() の戻り値を渡す想定）。
"""

import json
import re
from collections import Counter

import pandas as pd

# ---------------------------------------------------------------- 分類ルール

# (カテゴリ名, 正規表現) コメントは短文なのでキーワード一致で十分に切れる。
_CATEGORY_RULES = [
    ("自力宣言", r"自力"),
    ("番手宣言（名前あり）", r"(君|さん|選手)"),
    ("単騎", r"単騎"),
    ("好調アピール", r"仕上が|好調|絶好調|動ける|上向き|手応え|いい感じ|良い感じ|自信"),
    ("不安・不調", r"疲れ|よくない|良くない|重い|いまいち|イマイチ|不安|厳しい|ひと息"),
    ("地元", r"地元"),
    ("逃げ宣言", r"逃げ|先行"),
    ("捲り宣言", r"捲"),
    ("差し・追込宣言", r"差し|差す|追い込"),
    ("様子見・流れ任せ", r"様子|流れに|無欲|展開次第"),
]

CATEGORIES = [name for name, _ in _CATEGORY_RULES]


def classify_comment(text):
    """コメント本文 -> 当てはまるカテゴリ名のリスト（複数可）。"""
    if text is None:
        return []
    t = str(text).strip()
    if t in ("", "-", "nan", "None"):
        return []
    return [name for name, pat in _CATEGORY_RULES if re.search(pat, t)]


def normalize_phrase(text):
    """原文フレーズ集計用に表記をそろえる（前後空白・句読点の除去）。"""
    if text is None:
        return ""
    t = str(text).strip()
    t = t.replace("　", " ")
    t = re.sub(r"[。．、,\s]+$", "", t)
    return t


# ---------------------------------------------------------------- 払戻パース

def parse_payouts(payout_json_raw):
    """払戻金JSON -> {"2車単": {(1着,2着): 円}, "ワイド": {(小,大): 円}, ...}"""
    out = {"2車単": {}, "ワイド": {}, "3連単": {}, "3連複": {}}
    if payout_json_raw is None:
        return out
    try:
        p_list = (
            json.loads(payout_json_raw)
            if isinstance(payout_json_raw, str)
            else payout_json_raw
        )
    except Exception:
        return out
    if not isinstance(p_list, list):
        return out
    for item in p_list:
        if not isinstance(item, dict):
            continue
        b_type = item.get("賭け式")
        if b_type not in out:
            continue
        cars = [int(c) for c in re.findall(r"\d+", str(item.get("組番", "")))]
        amt_str = str(item.get("払戻金", ""))
        amt = int(re.sub(r"\D", "", amt_str)) if re.sub(r"\D", "", amt_str) else 0
        if b_type == "2車単" and len(cars) >= 2:
            out["2車単"][(cars[0], cars[1])] = amt
        elif b_type == "ワイド" and len(cars) >= 2:
            out["ワイド"][tuple(sorted(cars[:2]))] = amt
        elif b_type == "3連単" and len(cars) >= 3:
            out["3連単"][tuple(cars[:3])] = amt
        elif b_type == "3連複" and len(cars) >= 3:
            out["3連複"][tuple(sorted(cars[:3]))] = amt
    return out


# ---------------------------------------------------------------- 土台づくり

def coverage(df_players):
    """コメント収録状況の要約を返す（件数・割合・期間）。"""
    info = {"総選手行": 0, "コメントあり": 0, "コメント率%": 0.0,
            "レース数": 0, "最古レースID": "-", "最新レースID": "-"}
    if df_players is None or df_players.empty or "コメント" not in df_players.columns:
        return info
    info["総選手行"] = len(df_players)
    mask = df_players["コメント"].apply(
        lambda v: str(v).strip() not in ("", "-", "nan", "None")
    )
    info["コメントあり"] = int(mask.sum())
    if info["総選手行"]:
        info["コメント率%"] = round(100.0 * info["コメントあり"] / info["総選手行"], 1)
    if "レースID" in df_players.columns:
        ids = sorted(df_players.loc[mask, "レースID"].astype(str).unique())
        info["レース数"] = len(ids)
        if ids:
            info["最古レースID"] = ids[0]
            info["最新レースID"] = ids[-1]
    return info


def build_frame(df_players, df_results):
    """
    player_stats × race_results を結合し、1行=1選手（コメントあり）の
    分析用 DataFrame を返す。結果未確定レースも含む（確定フラグで区別）。

    列: レースID, 開催日, 会場, レース番号, 車, 選手名, 脚質, 競走得点, AI,
        コメント, フレーズ, カテゴリ(list), 着順(1/2/3/0=着外/None=未確定),
        出走数(そのレースの車数), 結果確定(bool),
        simA投資, simA払戻, simB投資, simB払戻
      simA = その選手軸の2車単総流し（相手は他全車・各100円）
      simB = その選手軸のワイド総流し（同上。当たれば上位3着内の相手2点分）
    """
    cols = ["レースID", "開催日", "会場", "レース番号", "車", "選手名", "脚質",
            "競走得点", "AI", "コメント", "フレーズ", "カテゴリ", "着順",
            "出走数", "結果確定", "simA投資", "simA払戻", "simB投資", "simB払戻"]
    if df_players is None or df_players.empty:
        return pd.DataFrame(columns=cols)
    if "コメント" not in df_players.columns or "レースID" not in df_players.columns:
        return pd.DataFrame(columns=cols)

    # 結果マップ: レースID -> (1着,2着,3着, payouts)
    res_map = {}
    if df_results is not None and not df_results.empty and "レースID" in df_results.columns:
        for _, r in df_results.iterrows():
            try:
                c1 = int(r.get("1着車番")) if pd.notnull(r.get("1着車番")) else None
                c2 = int(r.get("2着車番")) if pd.notnull(r.get("2着車番")) else None
                c3 = int(r.get("3着車番")) if pd.notnull(r.get("3着車番")) else None
            except Exception:
                c1 = c2 = c3 = None
            if c1 is None:
                continue
            res_map[str(r["レースID"])] = (c1, c2, c3, parse_payouts(r.get("払戻金JSON")))

    # レースごとの車数
    car_counts = df_players.groupby("レースID")["車"].count().to_dict()

    records = []
    for _, p in df_players.iterrows():
        comment = p.get("コメント")
        if str(comment).strip() in ("", "-", "nan", "None"):
            continue
        race_id = str(p.get("レースID"))
        parts = race_id.split("_")
        date_str = parts[0] if len(parts) >= 1 else "-"
        venue = parts[1] if len(parts) >= 2 else "-"
        r_num = parts[2] if len(parts) >= 3 else "-"
        try:
            car = int(p.get("車"))
        except Exception:
            continue
        try:
            score = float(p.get("競走得点"))
        except Exception:
            score = None

        finish = None
        n_cars = int(car_counts.get(p.get("レースID"), 0))
        sim_a_inv = sim_a_ret = sim_b_inv = sim_b_ret = 0
        res = res_map.get(race_id)
        if res is not None and n_cars >= 2:
            c1, c2, c3, payouts = res
            if car == c1:
                finish = 1
            elif car == c2:
                finish = 2
            elif car == c3:
                finish = 3
            else:
                finish = 0
            opp = n_cars - 1
            sim_a_inv = opp * 100
            sim_b_inv = opp * 100
            if finish == 1:
                sim_a_ret = payouts["2車単"].get((car, c2), 0)
            if finish in (1, 2, 3):
                top3 = [c1, c2, c3]
                for other in top3:
                    if other != car:
                        sim_b_ret += payouts["ワイド"].get(tuple(sorted((car, other))), 0)

        records.append({
            "レースID": race_id,
            "開催日": date_str,
            "会場": venue,
            "レース番号": r_num,
            "車": car,
            "選手名": p.get("選手名", "-"),
            "脚質": p.get("脚質", "-"),
            "競走得点": score,
            "AI": p.get("AI", "-"),
            "コメント": str(comment).strip(),
            "フレーズ": normalize_phrase(comment),
            "カテゴリ": classify_comment(comment),
            "着順": finish,
            "出走数": n_cars,
            "結果確定": finish is not None,
            "simA投資": sim_a_inv,
            "simA払戻": sim_a_ret,
            "simB投資": sim_b_inv,
            "simB払戻": sim_b_ret,
        })
    return pd.DataFrame(records, columns=cols)


def settled_only(df):
    if df is None or df.empty:
        return df
    return df[df["結果確定"] == True].copy()  # noqa: E712


# ---------------------------------------------------------------- 集計

def _agg(sub):
    n = len(sub)
    if n == 0:
        return None
    f = sub["着順"]
    inv_a = sub["simA投資"].sum()
    ret_a = sub["simA払戻"].sum()
    inv_b = sub["simB投資"].sum()
    ret_b = sub["simB払戻"].sum()
    return {
        "件数": n,
        "1着率%": round(100.0 * (f == 1).mean(), 1),
        "連対率%": round(100.0 * f.isin([1, 2]).mean(), 1),
        "3連対率%": round(100.0 * f.isin([1, 2, 3]).mean(), 1),
        "2車単流し回収率%": round(100.0 * ret_a / inv_a, 1) if inv_a else 0.0,
        "ワイド流し回収率%": round(100.0 * ret_b / inv_b, 1) if inv_b else 0.0,
    }


def _reliability(n):
    if n >= 300:
        return "高"
    if n >= 100:
        return "中"
    if n >= 50:
        return "低め"
    return "参考（少数）"


def phrase_stats(df, min_count=30):
    """コメント原文フレーズごとの成績。結果確定分のみを渡すこと。"""
    if df is None or df.empty:
        return pd.DataFrame()
    rows = []
    for phrase, sub in df.groupby("フレーズ"):
        if not phrase or len(sub) < min_count:
            continue
        agg = _agg(sub)
        if agg:
            rows.append({"コメント原文": phrase, "信頼度": _reliability(len(sub)), **agg})
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            .sort_values(["2車単流し回収率%", "件数"], ascending=[False, False])
            .reset_index(drop=True))


def category_stats(df):
    """カテゴリ（複数ラベル）ごとの成績。結果確定分のみを渡すこと。"""
    if df is None or df.empty:
        return pd.DataFrame()
    rows = []
    for cat in CATEGORIES:
        sub = df[df["カテゴリ"].apply(lambda cats: cat in cats)]
        agg = _agg(sub)
        if agg:
            rows.append({"カテゴリ": cat, "信頼度": _reliability(len(sub)), **agg})
    base = _agg(df)
    if base:
        rows.append({"カテゴリ": "（全コメント平均）", "信頼度": "-", **base})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["2車単流し回収率%"], ascending=False).reset_index(drop=True)


def _segment_values(df):
    """法則探索用の条件列を作る。"""
    df = df.copy()
    df["条件_脚質"] = df["脚質"].fillna("-")
    df["条件_会場"] = df["会場"].fillna("-")
    df["条件_車番帯"] = df["車"].apply(
        lambda c: "1〜3番車" if c <= 3 else ("4〜6番車" if c <= 6 else "7〜9番車"))
    df["条件_AI印"] = df["AI"].apply(
        lambda a: "◎○" if a in ("◎", "○") else ("▲△×注" if a in ("▲", "△", "×", "注") else "無印・その他"))

    def score_band(s):
        if s is None or (isinstance(s, float) and pd.isna(s)):
            return "得点不明"
        if s >= 105:
            return "得点105以上"
        if s >= 95:
            return "得点95〜104"
        return "得点94以下"

    df["条件_得点帯"] = df["競走得点"].apply(score_band)
    return df


_SEGMENT_COLS = ["条件_脚質", "条件_会場", "条件_車番帯", "条件_AI印", "条件_得点帯"]


def rule_candidates(df, min_samples=50, top_n=30):
    """
    カテゴリ × 条件 の組み合わせから回収率の高い法則候補を抽出。
    結果確定分のみを渡すこと。全体平均より良い組み合わせだけを返す。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    df = _segment_values(df)
    base = _agg(df)
    if not base:
        return pd.DataFrame()
    base_a = base["2車単流し回収率%"]
    base_b = base["ワイド流し回収率%"]

    rows = []
    for cat in CATEGORIES:
        cat_mask = df["カテゴリ"].apply(lambda cats: cat in cats)
        for col in _SEGMENT_COLS:
            for val, sub_all in df[cat_mask].groupby(col):
                if len(sub_all) < min_samples:
                    continue
                agg = _agg(sub_all)
                if not agg:
                    continue
                # どちらかの流し回収率が全体平均を10ポイント以上上回るものだけ
                if agg["2車単流し回収率%"] < base_a + 10 and agg["ワイド流し回収率%"] < base_b + 10:
                    continue
                rows.append({
                    "法則候補": f"{cat} × {col.replace('条件_', '')}={val}",
                    "カテゴリ": cat,
                    "条件": f"{col.replace('条件_', '')}={val}",
                    "信頼度": _reliability(len(sub_all)),
                    **agg,
                })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["良さ"] = out[["2車単流し回収率%", "ワイド流し回収率%"]].max(axis=1)
    return (out.sort_values(["良さ", "件数"], ascending=[False, False])
            .head(top_n).drop(columns=["良さ"]).reset_index(drop=True))


def phrase_examples(df, phrase=None, keyword=None, limit=100):
    """実例表示用：フレーズ完全一致 or キーワード部分一致の行を返す。"""
    if df is None or df.empty:
        return pd.DataFrame()
    sub = df
    if phrase:
        sub = sub[sub["フレーズ"] == phrase]
    if keyword:
        sub = sub[sub["コメント"].str.contains(re.escape(keyword), na=False)]
    cols = ["開催日", "会場", "レース番号", "車", "選手名", "脚質", "競走得点",
            "コメント", "着順"]
    return sub.sort_values(["開催日", "レースID"], ascending=False)[cols].head(limit)


def top_keywords(df, min_count=20):
    """コメントに頻出する単語（2〜4文字の漢字・カナ塊）の成績つき一覧。"""
    if df is None or df.empty:
        return pd.DataFrame()
    counter = Counter()
    token_rows = {}
    for _, row in df.iterrows():
        tokens = set(re.findall(r"[ァ-ヶー一-龥]{2,4}", str(row["コメント"])))
        for tok in tokens:
            counter[tok] += 1
            token_rows.setdefault(tok, []).append(row.name)
    rows = []
    for tok, cnt in counter.most_common():
        if cnt < min_count:
            continue
        agg = _agg(df.loc[token_rows[tok]])
        if agg:
            rows.append({"頻出ワード": tok, **agg})
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            .sort_values(["2車単流し回収率%", "件数"], ascending=[False, False])
            .reset_index(drop=True))
