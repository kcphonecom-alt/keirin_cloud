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
    parts = re.split(r"[-=]", text.strip())
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
    print("pro_trends self-test OK")
