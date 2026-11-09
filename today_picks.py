"""「今日のおすすめ買い目」ページの選別ロジック。

ev_v2 の候補（確率モデル＋PRO支持）に、Kドリームス記者の◎との
一致情報を重ねて、その日の「おすすめ（本命）」を選ぶ。

選別の考え方（2026-11-10 改定）:
旧ルールは本命に「ふるだび支持＋記者の◎と軸が一致＋推定回収率100%以上」の
三重条件を課したため、本命がほとんど出なかった（◎との完全一致自体が
全期間で95レースしかない）。おすすめ枠もてんぐり軸限定だったため、
出てくる買い目がてんぐりの内容に偏っていた。
そこで、予想家や軸で先に絞るのをやめ、候補全体から「本当に期待値の高い
買い目」を出す形に改めた:
- 候補全体から推定回収率100%以上のものを母集団にし、1レース1買い目
  （そのレースで推定回収率が一番高いもの）に絞る。
- おすすめ（本命）: 母集団のうち推定回収率110%以上・推定的中率10%以上で、
  根拠の重なり（PRO複数名の支持／ふるだび支持／記者の◎一致 のいずれか）が
  あるものから、推定回収率の高い順に最大2件。
- おすすめ: 本命に入らなかった母集団から、推定回収率の高い順に最大3件。
  予想家は問わない（てんぐりも含むが、てんぐり限定ではない）。
- 根拠の重なりは「絞り込み」ではなく、本命の最低条件と同点時の優先、
  そして根拠表示にだけ使う。
- 一撃: 将来、本物の高配当だけを分かりやすく出す枠として、実オッズを使う
  形で後日作り直す予定。現在は選別・表示とも無し。
推定回収率はモデルの目安であり、最終判定は実オッズで EV監視
（ev_watch.py）が行う、という役割分担は変わらない。

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
              "予想家", "予想家人数", "推定回収率%", "推定的中率%", "記者◎との関係"]


def _to_list(d):
    return [{k: r[k] for k in _PICK_COLS if k in r} for _, r in d.iterrows()]


def supporter_count(row):
    """その買い目を支持するPROの人数。列が無ければ予想家文字列から数える。"""
    n = row.get("予想家人数")
    if n is not None and not (isinstance(n, float) and pd.isna(n)):
        try:
            return int(n)
        except (TypeError, ValueError):
            pass
    authors = str(row.get("予想家", "") or "")
    if not authors:
        return 0
    return len([a for a in authors.split("、") if a.strip()])


def support_score(row):
    """根拠の重なり（複数PRO支持+2 / ふるだび支持+1 / 記者◎一致+1）。"""
    score = 0
    if supporter_count(row) >= 2:
        score += 2
    if "ふるだび" in str(row.get("予想家", "") or ""):
        score += 1
    if row.get("記者◎との関係") == "一致":
        score += 1
    return score


def select_picks(ranked, kd_by_race, max_main=2, max_sub=3):
    """おすすめ（本命）とおすすめを選んで dict で返す。ranked は ev_v2 の候補 DataFrame。

    予想家や軸では絞らず、推定回収率100%以上の候補全体から
    「期待値の高い買い目」を出す（2026-11-10 改定、モジュール冒頭参照）。
    """
    if ranked is None or ranked.empty:
        return {"本命": [], "おすすめ": []}
    df = enrich(ranked, kd_by_race)
    df = df[df["推定回収率%"] >= 100].copy()
    if df.empty:
        return {"本命": [], "おすすめ": []}
    df["_根拠"] = df.apply(support_score, axis=1)
    # 1レース1買い目（そのレースで推定回収率が一番高いもの）
    df = df.sort_values(
        ["推定回収率%", "_根拠", "推定的中率%"],
        ascending=False).drop_duplicates("レースID", keep="first")

    order = ["推定回収率%", "_根拠", "推定的中率%"]
    main_base = df[(df["推定回収率%"] >= 110)
                   & (df["推定的中率%"] >= 10)
                   & (df["_根拠"] >= 1)]
    main = main_base.sort_values(order, ascending=False).head(max_main)

    used = set(main["レースID"]) if len(main) else set()
    sub = df[~df["レースID"].isin(used)].sort_values(
        order, ascending=False).head(max_sub)
    return {"本命": _to_list(main), "おすすめ": _to_list(sub)}


def picks_signature(picks):
    """買い目の顔ぶれを表す署名（保存履歴の重複判定用）。

    レース・券種・買い目・予想家が同じなら、推定値の細かい変動では
    署名は変わらない。顔ぶれが変わった時だけ、新しい保存が要る。
    """
    items = []
    for kind in ("本命", "おすすめ"):
        for p in picks.get(kind, []) or []:
            items.append([
                kind,
                str(p.get("レースID", "")),
                str(p.get("券種", "")),
                str(p.get("買い目", "")),
                str(p.get("予想家", "")),
            ])
    return json.dumps(sorted(items), ensure_ascii=False)


def _jsonable(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


def to_jsonable_picks(picks):
    """保存用に、買い目dictの値を JSON 化できる型（str/float）にそろえる。"""
    out = {}
    for kind in ("本命", "おすすめ"):
        out[kind] = [
            {k: _jsonable(v) for k, v in p.items()}
            for p in picks.get(kind, []) or []
        ]
    return out


def reason_text(pick, kind):
    """買い目の根拠を短い文で返す（ページ表示用）。"""
    parts = []
    authors = str(pick.get("予想家", "") or "")
    n_authors = supporter_count(pick)
    if n_authors >= 2:
        parts.append(f"PRO{n_authors}名が支持")
    if "ふるだび" in authors:
        parts.append("ふるだびが支持")
    if "てんぐり" in authors:
        parts.append("てんぐりが支持")
    rel = pick.get("記者◎との関係", "")
    if rel == "一致":
        parts.append("Kドリームス記者の◎とも軸が一致")
    elif rel == "不一致":
        parts.append("記者の◎とは軸がズレている")
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
    assert len(picks["本命"]) == 1 and picks["本命"][0]["買い目"] == "1-2"        # ふるだび+◎一致+EV130
    assert len(picks["おすすめ"]) == 1 and picks["おすすめ"][0]["買い目"] == "3-2"  # てんぐり単独でも高EVならおすすめに出る
    assert "ふるだびが支持" in reason_text(picks["本命"][0], "本命")

    # 新ルールの要: ふるだびでなくても、複数PRO支持＋高EVなら本命に出る
    ranked2 = pd.DataFrame([
        {"レースID": "20261106_松山_4R_d3", "会場": "松山", "発走時間": "21:40", "券種": "2車単",
         "買い目": "2-3", "予想家": "Y、133さん", "予想家人数": 2, "推定回収率%": 150.0, "推定的中率%": 20.0},
        {"レースID": "20261106_松山_4R_d3", "会場": "松山", "発走時間": "21:40", "券種": "ワイド",
         "買い目": "2=3", "予想家": "ふるだび", "予想家人数": 1, "推定回収率%": 120.0, "推定的中率%": 40.0},
        {"レースID": "20261106_松山_5R_d3", "会場": "松山", "発走時間": "22:00", "券種": "2車単",
         "買い目": "4-1", "予想家": "カズヤ丸", "予想家人数": 1, "推定回収率%": 180.0, "推定的中率%": 18.0},
        {"レースID": "20261106_松山_6R_d3", "会場": "松山", "発走時間": "22:20", "券種": "2車単",
         "買い目": "5-1", "予想家": "てんぐり", "予想家人数": 1, "推定回収率%": 105.0, "推定的中率%": 30.0},
    ])
    kd2 = {"20261106_松山_4R_d3": 2, "20261106_松山_5R_d3": 1, "20261106_松山_6R_d3": 1}
    picks2 = select_picks(ranked2, kd2)
    # 4Rは1レース1買い目でEVが高い 2-3 が残り、複数支持+◎一致で本命になる
    assert len(picks2["本命"]) == 1 and picks2["本命"][0]["買い目"] == "2-3"
    assert "PRO2名が支持" in reason_text(picks2["本命"][0], "本命")
    # 5RはEV180でも根拠の重なりが無いので本命ではなく、おすすめの先頭に出る
    assert picks2["おすすめ"][0]["買い目"] == "4-1"
    assert len(picks2["おすすめ"]) == 2  # 5R と 6R（EV105）

    sig = picks_signature(picks)
    assert picks_signature(picks) == sig
    changed = {"本命": [dict(picks["本命"][0], 買い目="1-3")], "おすすめ": []}
    assert picks_signature(changed) != sig
    # 推定値だけの変動では署名は変わらない
    wiggle = {"本命": [dict(picks["本命"][0], **{"推定回収率%": 131.5})],
              "おすすめ": list(picks["おすすめ"])}
    assert picks_signature(wiggle) == sig
    jp = to_jsonable_picks(picks)
    json.dumps(jp, ensure_ascii=False)  # 例外なくJSON化できること
    assert jp["本命"][0]["買い目"] == "1-2"
    assert jp["おすすめ"][0]["買い目"] == "3-2"
    print("today_picks self-test OK")
