"""「今日のおすすめ買い目」ページの選別ロジック。

ev_v2 の候補（確率モデル＋PRO支持）に、Kドリームス記者の◎との
一致情報を重ねて、その日の「おすすめ（本命）」を選ぶ。

選別の考え方（過去データの検証に基づく）:
- おすすめ: ふるだび支持 かつ 記者の◎と軸が一致 かつ 推定回収率100%以上。
  ふるだびは◎と軸が完全一致したレースで回収率が大きく上がった実績がある
  （織り交ぜ分析ページ参照）。足りない場合は「◎一致＋推定回収率100%以上」
  の候補で補う。
- 一撃（表示停止中）: 高配当狙いとして実装したが、選ばれる買い目が実際には
  高配当にならなかったため、2026-11-08にページでの表示を止めた。
  後日ロジックから組み立て直す。以下は当時の考え方の記録。
  実オッズが無いこのアプリでは、PRO予想家てんぐりの
  買い目を軸にする（的中率25%以下の「穴」の範囲に絞り、本命級は除く）。
  Kドリームス記者の◎と軸が一致するものを優先し、残りは推定回収率の
  高い順に補う。

このモジュールは確率計算を持たない（ev_v2 の出力を入力にする）。
"""

import json
import re
from datetime import datetime, timedelta

import pandas as pd


def actual_date_of(race_id):
    """レースID（初日_会場_NR_d日目）から実際のレース日（YYYYMMDD）を返す。"""
    try:
        parts = str(race_id).split("_")
        start = datetime.strptime(parts[0], "%Y%m%d")
        day = int(parts[3].lstrip("d"))
        return (start + timedelta(days=day - 1)).strftime("%Y%m%d")
    except Exception:
        return None


def payout_for(payout_json_raw, bet_type, combo):
    """払戻金JSONから、その買い目（1点）の払戻金を返す。外れ・不明は0。"""
    if payout_json_raw is None:
        return 0
    try:
        items = (json.loads(payout_json_raw)
                 if isinstance(payout_json_raw, str) else payout_json_raw)
    except Exception:
        return 0
    if not isinstance(items, list):
        return 0
    want = [int(x) for x in re.findall(r"\d+", str(combo))]
    for item in items:
        if item.get("賭け式") != bet_type:
            continue
        cars = [int(c) for c in re.findall(r"\d+", str(item.get("組番", "")))]
        amt = int(re.sub(r"\D", "", str(item.get("払戻金", ""))) or 0)
        if bet_type == "ワイド":
            if sorted(cars) == sorted(want):
                return amt
        elif cars == want:
            return amt
    return 0


def enrich(ranked, kd_by_race):
    """候補に「記者◎との関係」（一致／不一致／印なし）を付ける。

    kd_by_race: {race_id: ◎の車番}
    2車単は軸（1着側）が◎と一致するか、ワイドは◎が買い目に含まれるかで判定。
    """
    if ranked is None or ranked.empty:
        return ranked
    df = ranked.copy()

    def agree(row):
        hon = kd_by_race.get(row["レースID"])
        if hon is None:
            return "印なし"
        nums = [int(x) for x in re.findall(r"\d+", str(row["買い目"]))]
        if not nums:
            return "印なし"
        if row["券種"] == "2車単":
            return "一致" if nums[0] == int(hon) else "不一致"
        return "一致" if int(hon) in nums else "不一致"

    df["記者◎との関係"] = df.apply(agree, axis=1)
    return df


_PICK_COLS = ["レースID", "会場", "発走時間", "券種", "買い目",
              "予想家", "推定回収率%", "推定的中率%", "記者◎との関係"]


def _to_list(d):
    return [{k: r[k] for k in _PICK_COLS if k in r} for _, r in d.iterrows()]


def select_picks(ranked, kd_by_race, max_main=3, max_hit=2):
    """おすすめ／一撃を選んで dict で返す。ranked は ev_v2 の候補 DataFrame。"""
    if ranked is None or ranked.empty:
        return {"おすすめ": [], "一撃": []}
    df = enrich(ranked, kd_by_race)

    agree = df["記者◎との関係"] == "一致"
    ev_ok = df["推定回収率%"] >= 100
    furu = df["予想家"].str.contains("ふるだび", na=False)

    main = df[agree & ev_ok & furu].sort_values(
        ["推定的中率%", "推定回収率%"], ascending=False).head(max_main)
    if len(main) < max_main:
        extra = df[agree & ev_ok & ~df.index.isin(main.index)].sort_values(
            ["推定的中率%", "推定回収率%"], ascending=False).head(max_main - len(main))
        main = pd.concat([main, extra])

    used = set(main["レースID"]) if len(main) else set()
    # 一撃は「高配当狙い」。軸はPRO予想家てんぐりの買い目。
    # 的中率25%以下の穴の範囲に絞り（本命級はおすすめとの違いが出ないので除く）、
    # Kドリームス記者の◎と軸が一致するものを優先し、残りは推定回収率順に補う。
    tengu = df["予想家"].str.contains("てんぐり", na=False)
    hit_base = df[tengu
                  & (df["推定回収率%"] >= 100)
                  & (df["推定的中率%"] <= 25)
                  & (~df["レースID"].isin(used))]
    hit = hit_base[hit_base["記者◎との関係"] == "一致"].sort_values(
        "推定回収率%", ascending=False).head(max_hit)
    if len(hit) < max_hit:
        extra = hit_base[~hit_base.index.isin(hit.index)].sort_values(
            "推定回収率%", ascending=False).head(max_hit - len(hit))
        hit = pd.concat([hit, extra])
    return {"おすすめ": _to_list(main), "一撃": _to_list(hit)}


def reason_text(pick, kind):
    """買い目の根拠を短い文で返す（ページ表示用）。"""
    parts = []
    authors = str(pick.get("予想家", "") or "")
    if "ふるだび" in authors:
        parts.append("ふるだびが支持")
    if "てんぐり" in authors:
        parts.append("てんぐりが支持")
    rel = pick.get("記者◎との関係", "")
    if rel == "一致":
        parts.append("Kドリームス記者の◎とも軸が一致")
    elif rel == "不一致":
        parts.append("記者の◎とはズレた穴目")
    parts.append(f"モデル的中率 {pick.get('推定的中率%', '-')}％")
    parts.append(f"推定回収率 {pick.get('推定回収率%', '-')}％")
    return "／".join(parts)


if __name__ == "__main__":
    assert actual_date_of("20261106_松山_4R_d3") == "20261108"
    assert actual_date_of("20241104_青森_1R_d1") == "20241104"
    pj = json.dumps([
        {"賭け式": "2車単", "組番": "1-2", "払戻金": "1,050"},
        {"賭け式": "ワイド", "組番": "1=2", "払戻金": "320"},
    ], ensure_ascii=False)
    assert payout_for(pj, "2車単", "1-2") == 1050
    assert payout_for(pj, "2車単", "2-1") == 0
    assert payout_for(pj, "ワイド", "1=2") == 320
    assert payout_for(pj, "ワイド", "2=1") == 320  # ワイドは順序なし

    ranked = pd.DataFrame([
        {"レースID": "20261106_松山_4R_d3", "会場": "松山", "発走時間": "21:40", "券種": "2車単",
         "買い目": "1-2", "予想家": "ふるだび", "推定回収率%": 130.0, "推定的中率%": 20.0},
        {"レースID": "20261106_松山_5R_d3", "会場": "松山", "発走時間": "22:00", "券種": "2車単",
         "買い目": "3-2", "予想家": "てんぐり", "推定回収率%": 260.0, "推定的中率%": 9.0},
        {"レースID": "20261106_松山_6R_d3", "会場": "松山", "発走時間": "22:20", "券種": "ワイド",
         "買い目": "1=2", "予想家": "ふるだび", "推定回収率%": 90.0, "推定的中率%": 50.0},
    ])
    kd = {"20261106_松山_4R_d3": 1, "20261106_松山_5R_d3": 1, "20261106_松山_6R_d3": 1}
    picks = select_picks(ranked, kd)
    assert len(picks["おすすめ"]) == 1 and picks["おすすめ"][0]["買い目"] == "1-2"  # ふるだび+◎一致+EV>=100
    assert len(picks["一撃"]) == 1 and picks["一撃"][0]["買い目"] == "3-2"          # てんぐり+◎不一致+高EV
    assert "ふるだびが支持" in reason_text(picks["おすすめ"][0], "おすすめ")
    print("today_picks self-test OK")
