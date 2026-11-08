"""PRO×Kドリームス 織り交ぜ分析のための計算部品。

get_parsed_df() が返す DataFrame（予想家・買い目原文・結果着順・投資額・
払戻合計・券種別払戻）と、kdreams_marks テーブル（race_id・reporter・
honmei=◎・taikou=○）を突き合わせ、「PROの軸と記者の◎が一致した
レースだけに絞ると、PRO買い目の回収率はどう変わるか」を集計する。

記者の◎は単独では回収率70%前後（2024-11〜2026-10の54,780レースで検証済み）
だが、◎の1着率46.1%は「エースを当てる指標」としては本物。
買い目の源ではなく、PRO予想の確認・絞り込みに使えるかを見るための分析。
"""

import re

import pandas as pd

FINISH_RE = re.compile(r"^\d+-\d+-\d+$")


def parse_formation(text):
    """買い目原文（例 '3-17-1745'）を3列の車番集合に分解する。分解不能は None。"""
    if not isinstance(text, str) or not text.strip():
        return None
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


def parse_finish(text):
    """結果着順（例 '3-1-5'）を (1着, 2着, 3着) に分解。未確定などは None。"""
    if isinstance(text, str) and FINISH_RE.match(text.strip()):
        a, b, c = text.strip().split("-")
        return (int(a), int(b), int(c))
    return None


def settled_only(df):
    if df is None or df.empty:
        return df
    return df[df["結果着順"].apply(lambda s: parse_finish(s) is not None)].copy()


def _rec(ret, inv):
    return (ret / inv * 100) if inv and inv > 0 else 0.0


def merge_frames(df_parsed, df_kd):
    """結果確定のPRO行にKD印を結合し、軸一致フラグを付けた DataFrame を返す。

    df_kd は kdreams_marks テーブルの列（race_id, reporter, honmei, taikou）
    を持つ DataFrame。軸一致 = 記者の◎がPROフォーメーションの1列目（軸）に
    含まれること。軸完全一致 = 軸がその1車だけのこと。
    """
    if df_parsed is None or df_parsed.empty or df_kd is None or df_kd.empty:
        return pd.DataFrame()
    df = settled_only(df_parsed)
    kd = df_kd.rename(columns={
        "race_id": "レースID", "reporter": "KD記者",
        "honmei": "KD本命", "taikou": "KD対抗",
    })
    m = df.merge(kd[["レースID", "KD記者", "KD本命", "KD対抗"]],
                 on="レースID", how="inner")
    if m.empty:
        return m

    def flags(row):
        cols = parse_formation(row["買い目原文"])
        hon = row["KD本命"]
        if cols is None or hon is None or (isinstance(hon, float) and pd.isna(hon)):
            return pd.Series({"軸一致": None, "軸完全一致": None})
        hon = int(hon)
        return pd.Series({
            "軸一致": hon in cols[0],
            "軸完全一致": cols[0] == {hon},
        })

    m = m.join(m.apply(flags, axis=1))
    m = m[m["軸一致"].notna()].copy()
    m["軸一致"] = m["軸一致"].astype(bool)
    m["軸完全一致"] = m["軸完全一致"].astype(bool)
    return m


def _group_stats(sub, label):
    inv = sub["投資額"].sum()
    ret = sub["払戻合計"].sum()
    return {
        "区分": label,
        "レース数": len(sub),
        "的中率%": round((sub["払戻合計"] > 0).mean() * 100, 1) if len(sub) else 0.0,
        "回収率%": round(_rec(ret, inv), 1),
        "2車単の寄与%": round(_rec(sub["2車単払戻"].sum(), inv), 1),
        "ワイドの寄与%": round(_rec(sub["ワイド払戻"].sum(), inv), 1),
        "平均点数": round(sub["総点数"].mean(), 1) if len(sub) else 0.0,
    }


def agreement_stats(merged, min_races=30):
    """予想家ごと＋全PRO合算で、軸一致／不一致に分けた成績表を返す。"""
    if merged is None or merged.empty:
        return pd.DataFrame()
    rows = []

    def add(df_sub, name):
        rows.append({"対象": name, **_group_stats(df_sub, "全レース")})
        rows.append({"対象": name, **_group_stats(df_sub[df_sub["軸一致"]], "軸が◎と一致")})
        rows.append({"対象": name, **_group_stats(df_sub[~df_sub["軸一致"]], "軸が◎と不一致")})
        rows.append({"対象": name, **_group_stats(df_sub[df_sub["軸完全一致"]], "軸が◎と完全一致（軸1車）")})

    add(merged, "全PRO合算")
    for author, g in merged.groupby("予想家"):
        if len(g) >= min_races:
            add(g, author)
    return pd.DataFrame(rows)


def kd_form_stats(merged):
    """共通レースにおけるKD◎自体の成績と、PRO軸との一致率。"""
    if merged is None or merged.empty:
        return {}
    races = merged.drop_duplicates("レースID")
    fin = races["結果着順"].apply(parse_finish)
    hon = races["KD本命"].astype(int)
    n = len(races)
    first = sum(1 for f, h in zip(fin, hon) if f and f[0] == h)
    top2 = sum(1 for f, h in zip(fin, hon) if f and h in f[:2])
    top3 = sum(1 for f, h in zip(fin, hon) if f and h in f)
    return {
        "共通レース数": n,
        "◎1着率%": round(first / n * 100, 1) if n else 0.0,
        "◎連対率%": round(top2 / n * 100, 1) if n else 0.0,
        "◎3連対率%": round(top3 / n * 100, 1) if n else 0.0,
        "PRO軸との一致率%": round(merged["軸一致"].mean() * 100, 1),
    }


def reporter_stats(merged, min_races=50):
    """KD記者ごと: ◎の成績と、軸一致／不一致時のPRO合算回収率。"""
    if merged is None or merged.empty:
        return pd.DataFrame()
    rows = []
    for rep, g in merged.groupby("KD記者"):
        races = g.drop_duplicates("レースID")
        if len(races) < min_races:
            continue
        fin = races["結果着順"].apply(parse_finish)
        hon = races["KD本命"].astype(int)
        n = len(races)
        first = sum(1 for f, h in zip(fin, hon) if f and f[0] == h)
        ag = g[g["軸一致"]]
        dis = g[~g["軸一致"]]
        rows.append({
            "記者": rep,
            "共通レース数": n,
            "◎1着率%": round(first / n * 100, 1) if n else 0.0,
            "軸一致のレース数": int(ag.drop_duplicates("レースID").shape[0]),
            "一致時 PRO合算回収率%": round(_rec(ag["払戻合計"].sum(), ag["投資額"].sum()), 1),
            "不一致時 PRO合算回収率%": round(_rec(dis["払戻合計"].sum(), dis["投資額"].sum()), 1),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("共通レース数", ascending=False)


if __name__ == "__main__":
    df = pd.DataFrame([
        {"予想家": "A", "開催会場": "別府", "レースID": "20260101_別府_1R_d1", "買い目原文": "1-2-3",
         "結果着順": "1-2-3", "総点数": 10, "投資額": 1000, "払戻合計": 3000,
         "3連単払戻": 2000, "3連複払戻": 500, "2車単払戻": 300, "ワイド払戻": 200},
        {"予想家": "A", "開催会場": "別府", "レースID": "20260101_別府_2R_d1", "買い目原文": "2-1-3",
         "結果着順": "1-2-3", "総点数": 10, "投資額": 1000, "払戻合計": 0,
         "3連単払戻": 0, "3連複払戻": 0, "2車単払戻": 0, "ワイド払戻": 0},
        {"予想家": "A", "開催会場": "別府", "レースID": "20260101_別府_3R_d1", "買い目原文": "12-3-4",
         "結果着順": "2-1-3", "総点数": 10, "投資額": 1000, "払戻合計": 500,
         "3連単払戻": 0, "3連複払戻": 0, "2車単払戻": 500, "ワイド払戻": 0},
        {"予想家": "A", "開催会場": "別府", "レースID": "20260101_別府_4R_d1", "買い目原文": "1-2-3",
         "結果着順": "未確定", "総点数": 10, "投資額": 1000, "払戻合計": 0,
         "3連単払戻": 0, "3連複払戻": 0, "2車単払戻": 0, "ワイド払戻": 0},
    ])
    kd = pd.DataFrame([
        {"race_id": "20260101_別府_1R_d1", "reporter": "記者X", "honmei": 1, "taikou": 2},
        {"race_id": "20260101_別府_2R_d1", "reporter": "記者X", "honmei": 1, "taikou": 2},
        {"race_id": "20260101_別府_3R_d1", "reporter": "記者X", "honmei": 2, "taikou": 3},
        {"race_id": "20260101_別府_4R_d1", "reporter": "記者X", "honmei": 1, "taikou": 2},
    ])
    m = merge_frames(df, kd)
    assert len(m) == 3  # 未確定の4R目は除外
    assert bool(m.iloc[0]["軸一致"]) and bool(m.iloc[0]["軸完全一致"])      # ◎1 ∈ 軸{1}、完全一致
    assert not bool(m.iloc[1]["軸一致"])                                   # ◎1 ∉ 軸{2}
    assert bool(m.iloc[2]["軸一致"]) and not bool(m.iloc[2]["軸完全一致"])  # ◎2 ∈ 軸{1,2}、完全一致ではない
    ag = agreement_stats(m, min_races=1)
    all_row = ag[(ag["対象"] == "全PRO合算") & (ag["区分"] == "全レース")].iloc[0]
    assert all_row["レース数"] == 3 and all_row["回収率%"] == round(3500 / 3000 * 100, 1)
    agree_row = ag[(ag["対象"] == "全PRO合算") & (ag["区分"] == "軸が◎と一致")].iloc[0]
    assert agree_row["レース数"] == 2 and agree_row["回収率%"] == round(3500 / 2000 * 100, 1)
    fs = kd_form_stats(m)
    assert fs["共通レース数"] == 3 and fs["◎1着率%"] == 100.0  # 3レースとも◎が1着（R2はPROの軸とは別）
    rs = reporter_stats(m, min_races=1)
    assert len(rs) == 1 and rs.iloc[0]["共通レース数"] == 3
    print("kdreams_mix self-test OK")
