"""PRO傾向＆会場別 勝ち筋分析のための計算部品。

get_parsed_df() が返す DataFrame（予想家・開催会場・買い目原文・結果着順・
総点数・投資額・払戻合計・券種別払戻）を入力に、会場別の得意／不得意、
買い方スタイル、PRO同士の相性（軸の一致／不一致）を集計する。

合成MIXフォーメーション（文字列）の生成もここで行い、実際の払戻金との
照合は app_cloud.py 側の parse_bet_string_detailed_rows で行う
（このモジュール自体は払戻データを持たない）。
"""

import re
from collections import Counter

import pandas as pd

FINISH_RE = re.compile(r"^\d+-\d+-\d+$")


def parse_formation(text):
    """買い目原文（例 '3-17-1745' / '45=45-12'）を3列の車番集合に分解する。

    分解できない場合は None。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    # 区切りは PRO によって表記が違う（'-' / '=' / 'ー' など）ため幅広く対応する
    parts = re.split(r"[-=ー－–—]", text.strip())
    if len(parts) != 3:
        return None
    cols = []
    for p in parts:
        digits = [int(ch) for ch in p if ch.isdigit()]
        if not digits:
            return None
        cols.append(set(digits))
    return cols


def formation_to_text(cols):
    """3列の車番集合を買い目原文形式（例 '1-27-345'）に戻す。"""
    return "-".join("".join(str(d) for d in sorted(c)) for c in cols)


def parse_finish(text):
    """結果着順（例 '3-1-5'）を (1着, 2着, 3着) に分解。未確定などは None。"""
    if isinstance(text, str) and FINISH_RE.match(text.strip()):
        a, b, c = text.strip().split("-")
        return (int(a), int(b), int(c))
    return None


def settled_only(df):
    """結果が確定している行だけを返す。"""
    if df is None or df.empty:
        return df
    return df[df["結果着順"].apply(lambda s: parse_finish(s) is not None)].copy()


def _rec(ret, inv):
    return (ret / inv * 100) if inv and inv > 0 else 0.0


def style_stats(df):
    """PROごとの買い方スタイル指標を返す（結果確定分のみ）。"""
    df = settled_only(df)
    rows = []
    for author, g in df.groupby("予想家"):
        parsed = [(parse_formation(t), parse_finish(f)) for t, f in zip(g["買い目原文"], g["結果着順"])]
        ok = [(c, f) for c, f in parsed if c is not None and f is not None]
        axis_single = axis_hit = cover = 0
        axis_cars = Counter()
        for cols, fin in ok:
            if len(cols[0]) == 1:
                axis_single += 1
            axis_cars.update(cols[0])
            if fin[0] in cols[0]:
                axis_hit += 1
            if fin[0] in cols[0] and fin[1] in cols[1] and fin[2] in cols[2]:
                cover += 1
        n_ok = max(1, len(ok))
        inv = g["投資額"].sum()
        rows.append({
            "予想家": author,
            "レース数": len(g),
            "的中レース率%": round((g["払戻合計"] > 0).mean() * 100, 1),
            "回収率%": round(_rec(g["払戻合計"].sum(), inv), 1),
            "平均点数": round(g["総点数"].mean(), 1),
            "軸1車率%": round(axis_single / n_ok * 100, 1),
            "軸の1着包含率%": round(axis_hit / n_ok * 100, 1),
            "着順カバー率%": round(cover / n_ok * 100, 1),
            "よく軸にする車番": ", ".join(f"{car}番({cnt}回)" for car, cnt in axis_cars.most_common(3)),
            "払戻の柱": max(
                [("3連単", g["3連単払戻"].sum()), ("3連複", g["3連複払戻"].sum()),
                 ("2車単", g["2車単払戻"].sum()), ("ワイド", g["ワイド払戻"].sum())],
                key=lambda x: x[1],
            )[0],
        })
    return pd.DataFrame(rows).sort_values("レース数", ascending=False)


def venue_stats(df, min_races=20):
    """PRO×会場の成績表（最低レース数以上）を回収率順に返す。"""
    df = settled_only(df)
    rows = []
    for (author, venue), g in df.groupby(["予想家", "開催会場"]):
        if len(g) < min_races:
            continue
        inv = g["投資額"].sum()
        ret = g["払戻合計"].sum()
        rows.append({
            "予想家": author,
            "会場": venue,
            "レース数": len(g),
            "的中レース率%": round((g["払戻合計"] > 0).mean() * 100, 1),
            "投資額": int(inv),
            "払戻額": int(ret),
            "回収率%": round(_rec(ret, inv), 1),
            "信頼度": "高" if len(g) >= 30 else "中",
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("回収率%", ascending=False)


def venue_pivot(df, min_races=20):
    """PROを行・会場を列にした回収率マトリクス（最低レース数未満は空欄）。"""
    vs = venue_stats(df, min_races=min_races)
    if vs.empty:
        return pd.DataFrame()
    return vs.pivot(index="予想家", columns="会場", values="回収率%")


def pair_frames(df, author_a, author_b):
    """2人のPROが同じレースに出した予想を横並びにした DataFrame を返す。"""
    df = settled_only(df)
    a = df[df["予想家"] == author_a][["レースID", "買い目原文", "投資額", "払戻合計"]]
    b = df[df["予想家"] == author_b][["レースID", "買い目原文", "投資額", "払戻合計"]]
    m = a.merge(b, on="レースID", suffixes=("_A", "_B"))
    if m.empty:
        return m
    m["cols_A"] = m["買い目原文_A"].apply(parse_formation)
    m["cols_B"] = m["買い目原文_B"].apply(parse_formation)
    m = m[m["cols_A"].notna() & m["cols_B"].notna()].copy()
    m["軸一致"] = m.apply(lambda r: len(r["cols_A"][0] & r["cols_B"][0]) > 0, axis=1)
    return m


def pair_summary(df, author_a, author_b):
    """軸の一致／不一致で分けた実績（両者の実際の買い目の合算成績）。"""
    m = pair_frames(df, author_a, author_b)
    if m.empty:
        return pd.DataFrame()

    def stats(sub, label):
        if sub.empty:
            return None
        inv_a, ret_a = sub["投資額_A"].sum(), sub["払戻合計_A"].sum()
        inv_b, ret_b = sub["投資額_B"].sum(), sub["払戻合計_B"].sum()
        return {
            "区分": label,
            "レース数": len(sub),
            f"{author_a}単独 回収率%": round(_rec(ret_a, inv_a), 1),
            f"{author_b}単独 回収率%": round(_rec(ret_b, inv_b), 1),
            "両方全買い 回収率%": round(_rec(ret_a + ret_b, inv_a + inv_b), 1),
            "どちらか的中率%": round(((sub["払戻合計_A"] > 0) | (sub["払戻合計_B"] > 0)).mean() * 100, 1),
        }

    rows = [stats(m, "全レース（共通）"),
            stats(m[m["軸一致"]], "軸が一致したレース"),
            stats(m[~m["軸一致"]], "軸が割れたレース")]
    return pd.DataFrame([r for r in rows if r])


def type_stats(df):
    """PROごとの「型」指標と自動分類を返す（結果確定分のみ）。

    型の判定ルール（上から順に適用）:
    - 大当たり寄与（単レース回収率300%以上の払戻が総払戻に占める割合）が
      55%以上 → 「一発型」
    - 的中率50%以上 かつ 的中時平均払戻1,600円以下 → 「安定・拡散型」
    - 的中率50%以上 → 「バランス型」
    - それ以外 → 「バランス型」
    """
    df = settled_only(df)
    rows = []
    for author, g in df.groupby("予想家"):
        g = g.sort_values("レースID")
        inv = g["投資額"].sum()
        ret = g["払戻合計"].sum()
        hits = g[g["払戻合計"] > 0]
        hit_rate = (g["払戻合計"] > 0).mean() * 100
        avg_hit = hits["払戻合計"].mean() if len(hits) else 0
        row_rate = g["払戻合計"] / g["投資額"].where(g["投資額"] > 0) * 100
        big = g[row_rate >= 300]
        big_share = (big["払戻合計"].sum() / ret * 100) if ret > 0 else 0.0
        streak = mx = 0
        for v in g["払戻合計"]:
            streak = streak + 1 if v == 0 else 0
            mx = max(mx, streak)
        if big_share >= 55:
            ptype = "一発型"
        elif hit_rate >= 50 and avg_hit <= 1600:
            ptype = "安定・拡散型"
        else:
            ptype = "バランス型"
        rows.append({
            "予想家": author,
            "型": ptype,
            "レース数": len(g),
            "回収率%": round(_rec(ret, inv), 1),
            "的中率%": round(hit_rate, 1),
            "的中時平均払戻": f"{avg_hit:,.0f}円",
            "最大払戻": f"{g['払戻合計'].max():,.0f}円",
            "大当たり回数（回収率300%+）": int(len(big)),
            "大当たり寄与%": round(big_share, 1),
            "最長無払戻連続": int(mx),
            "信頼度": "高" if len(g) >= 300 else ("中" if len(g) >= 100 else "参考（少数）"),
        })
    return pd.DataFrame(rows).sort_values("レース数", ascending=False)


SAT_CONDITIONS = ["常時", "得意会場のみ", "軸一致時のみ", "軸不一致時のみ"]


def strong_venues(df, author, min_races=15, min_rec=100.0):
    """そのPROの得意会場（最低レース数以上かつ回収率が基準以上）の集合。"""
    vs = venue_stats(df[df["予想家"] == author], min_races=min_races)
    if vs.empty:
        return set()
    return set(vs[vs["回収率%"] >= min_rec]["会場"])


def satellite_simulation(df, base_author, sat_author, share=0.2,
                         condition="得意会場のみ", budget=1000):
    """主軸PROにサテライトPROを予算按分で混ぜた場合の合成回収率を試算する。

    1レースの予算を budget 円とし、サテライト作動時は (1-share) を主軸、
    share をサテライトに配分（払戻は配分比率で比例計算）。作動しない
    レースは主軸に全額。共通レース（結果確定・両者とも予想あり）が対象。
    """
    m = pair_frames(df, base_author, sat_author)
    if m.empty:
        return {}
    venues_strong = strong_venues(df, sat_author) if condition == "得意会場のみ" else set()
    m = m.copy()
    m["会場"] = m["レースID"].str.split("_").str[1]
    if condition == "常時":
        m["作動"] = True
    elif condition == "得意会場のみ":
        m["作動"] = m["会場"].isin(venues_strong)
    elif condition == "軸一致時のみ":
        m["作動"] = m["軸一致"]
    elif condition == "軸不一致時のみ":
        m["作動"] = ~m["軸一致"]
    else:
        m["作動"] = True

    tot_cost = tot_ret = 0.0
    for _, r in m.iterrows():
        sa = budget * (1 - share) if r["作動"] else float(budget)
        sb = budget * share if r["作動"] else 0.0
        tot_cost += budget
        if r["投資額_A"]:
            tot_ret += r["払戻合計_A"] * sa / r["投資額_A"]
        if sb and r["投資額_B"]:
            tot_ret += r["払戻合計_B"] * sb / r["投資額_B"]
    return {
        "共通レース数": len(m),
        "作動レース数": int(m["作動"].sum()),
        "主軸単独 回収率%": round(_rec(m["払戻合計_A"].sum(), m["投資額_A"].sum()), 1),
        "サテライト単独 回収率%": round(_rec(m["払戻合計_B"].sum(), m["投資額_B"].sum()), 1),
        "合成 回収率%": round(_rec(tot_ret, tot_cost), 1),
    }


def mix_formations(cols_a, cols_b):
    """2人のフォーメーションから合成MIXパターンの買い目原文を作る。

    戻り値: {パターン名: 買い目原文 or None（作れない場合）}
    """
    inter_axis = cols_a[0] & cols_b[0]
    union2 = cols_a[1] | cols_b[1]
    union3 = cols_a[2] | cols_b[2]
    return {
        "軸一致ミックス（軸=共通, 相手=合算）":
            formation_to_text([inter_axis, union2, union3]) if inter_axis else None,
        "A軸×B相手ミックス":
            formation_to_text([cols_a[0], union2, union3]),
        "B軸×A相手ミックス":
            formation_to_text([cols_b[0], union2, union3]),
        "全合算ミックス（軸も相手も合算）":
            formation_to_text([cols_a[0] | cols_b[0], union2, union3]),
    }


if __name__ == "__main__":
    # --- 簡易セルフテスト（合成データ） ---
    assert parse_formation("3-17-1745") == [{3}, {1, 7}, {1, 7, 4, 5}]
    assert parse_formation("45=45-12") == [{4, 5}, {4, 5}, {1, 2}]
    assert parse_formation("1ー3ー234") == [{1}, {3}, {2, 3, 4}]
    assert parse_formation("") is None
    assert formation_to_text([{3}, {7, 1}, {5, 4}]) == "3-17-45"
    assert parse_finish("3-1-5") == (3, 1, 5)
    assert parse_finish("未確定") is None

    mix = mix_formations([{1}, {2, 7}, {3}], [{1, 5}, {2}, {3, 4}])
    assert mix["軸一致ミックス（軸=共通, 相手=合算）"] == "1-27-34"
    assert mix["A軸×B相手ミックス"] == "1-27-34"
    assert mix["B軸×A相手ミックス"] == "15-27-34"
    assert mix["全合算ミックス（軸も相手も合算）"] == "15-27-34"
    mix2 = mix_formations([{1}, {2}, {3}], [{5}, {2}, {3}])
    assert mix2["軸一致ミックス（軸=共通, 相手=合算）"] is None

    df = pd.DataFrame([
        {"予想家": "A", "開催会場": "別府", "レースID": "20260101_別府_1R_d1", "買い目原文": "1-2-3",
         "結果着順": "1-2-3", "総点数": 10, "投資額": 1000, "払戻合計": 3000,
         "3連単払戻": 2000, "3連複払戻": 500, "2車単払戻": 300, "ワイド払戻": 200},
        {"予想家": "A", "開催会場": "別府", "レースID": "20260101_別府_2R_d1", "買い目原文": "2-1-3",
         "結果着順": "1-2-3", "総点数": 10, "投資額": 1000, "払戻合計": 0,
         "3連単払戻": 0, "3連複払戻": 0, "2車単払戻": 0, "ワイド払戻": 0},
        {"予想家": "B", "開催会場": "別府", "レースID": "20260101_別府_1R_d1", "買い目原文": "1-3-2",
         "結果着順": "1-2-3", "総点数": 8, "投資額": 800, "払戻合計": 400,
         "3連単払戻": 0, "3連複払戻": 400, "2車単払戻": 0, "ワイド払戻": 0},
        {"予想家": "B", "開催会場": "別府", "レースID": "20260101_別府_2R_d1", "買い目原文": "3-1-2",
         "結果着順": "1-2-3", "総点数": 8, "投資額": 800, "払戻合計": 0,
         "3連単払戻": 0, "3連複払戻": 0, "2車単払戻": 0, "ワイド払戻": 0},
    ])
    vs = venue_stats(df, min_races=2)
    assert len(vs) == 2 and vs.iloc[0]["予想家"] == "A" and vs.iloc[0]["回収率%"] == 150.0
    ss = style_stats(df)
    assert ss.iloc[0]["軸の1着包含率%"] == 50.0
    ps = pair_summary(df, "A", "B")
    assert len(ps) == 3 and ps.iloc[1]["レース数"] == 1  # 軸一致は1レース目のみ
    ts = type_stats(df)
    assert set(ts["予想家"]) == {"A", "B"} and "型" in ts.columns
    sim = satellite_simulation(df, "A", "B", share=0.2, condition="常時")
    assert sim["共通レース数"] == 2 and sim["作動レース数"] == 2
    sim_v = satellite_simulation(df, "A", "B", share=0.2, condition="得意会場のみ")
    assert sim_v["作動レース数"] == 0  # 2レースでは得意会場の条件（15レース以上）を満たさない
    print("pro_trends self-test OK")
