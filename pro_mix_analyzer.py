import json
import os
import pandas as pd
import psycopg2

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres.rtlnseqyepazjhyhsvpx:Qpqpqpqpqp123!#@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres",
)


def get_db_connection():
  return psycopg2.connect(DATABASE_URL)


def parse_cars(val):
  """コンマ区切りやリストの文字列から数値のリストを安全に抽出する補助関数"""
  if not val:
    return []
  if isinstance(val, list):
    return [int(x) for x in val if str(x).isdigit()]
  if isinstance(val, str):
    cleaned = val.replace("[", "").replace("]", "").replace("'", "")
    nums = [s.strip() for s in cleaned.split(",") if s.strip()]
    return [int(x) for x in nums if x.isdigit()]
  return []


def run_pro_mix_analysis():
  try:
    conn = get_db_connection()
    cursor = conn.cursor()
  except Exception as e:
    return None, f"DB接続エラー: {e}"

  # 新しく整備した「Pro予想買い目1〜3」と「買い目原文」を確実に取得するクエリ
  query = """
        SELECT 
            r."レースID",
            r."1着車番", r."2着車番", r."3着車番",
            r."払戻金JSON",
            m."Pro予想買い目1",
            m."Pro予想買い目2",
            m."Pro予想買い目3",
            m."買い目原文"
        FROM race_results r
        JOIN race_memos m ON r."レースID" = m."レースID"
        WHERE r.is_canceled IS NOT TRUE
          AND r."払戻金JSON" IS NOT NULL
          AND r."払戻金JSON"::text != '[]'
          AND (m."Pro予想買い目1" IS NOT NULL OR m."Pro予想買い目2" IS NOT NULL)
    """

  try:
    df = pd.read_sql(query, conn)
  except Exception as e:
    conn.close()
    return None, f"データ取得エラー: {e}"

  conn.close()
  if df.empty:
    return None, "分析可能なレースデータ（Pro予想買い目データ）が存在しません。"

  results = {}

  for idx, row in df.iterrows():
    payouts = (
        json.loads(row["払戻金JSON"])
        if isinstance(row["払戻金JSON"], str)
        else row["払戻金JSON"]
    )
    payout_dict = {}
    if payouts:
      for p in payouts:
        val_str = str(p.get("払戻金", "")).replace("円", "").replace(",", "").strip()
        if val_str.isdigit():
          payout_dict[(p["賭け式"], p["組番"])] = int(val_str)

    # 1列目、2列目、3列目の車番リストを取得
    c1_list = parse_cars(row.get("Pro予想買い目1"))
    c2_list = parse_cars(row.get("Pro予想買い目2"))
    c3_list = parse_cars(row.get("Pro予想買い目3"))

    # もし3列目が空の場合は2列目をフォールバックとして利用
    if not c3_list:
      c3_list = c2_list

    if not c1_list or not c2_list:
      continue

    # --- パターン1: 3連単 (1列目 -> 2列目 -> 3列目 フォーメーション) ---
    sanrentan = []
    for c1 in c1_list:
      for c2 in c2_list:
        for c3 in c3_list:
          if len({c1, c2, c3}) == 3:  # 3車がすべて異なる場合
            sanrentan.append(f"{c1}-{c2}-{c3}")
    eval_pattern(
        "3連単",
        "1列目(1着)->2列目(2着)->3列目(3着)",
        set(sanrentan),
        payout_dict,
        results,
    )

    # --- パターン2: 2車単 (1列目 -> 2列目 流し) ---
    nifukutan = [f"{c1}-{c2}" for c1 in c1_list for c2 in c2_list if c1 != c2]
    eval_pattern(
        "2車単", "1列目(1着) -> 2列目(2着) 流し", set(nifukutan), payout_dict, results
    )

    # --- パターン3: 2車複 (1列目 ＋ 2列目 組み合わせBOX) ---
    nifuku = []
    box_cars_2 = list(set(c1_list + c2_list))
    for i in range(len(box_cars_2)):
      for j in range(i + 1, len(box_cars_2)):
        nifuku.append(f"{box_cars_2[i]}={box_cars_2[j]}")
    eval_pattern(
        "2車複", "1・2列目 統合BOX", set(nifuku), payout_dict, results
    )

    # --- パターン4: ワイド (1列目 ＋ 2列目 組み合わせBOX) ---
    eval_pattern("ワイド", "1・2列目 統合BOX", set(nifuku), payout_dict, results)

    # --- パターン5: 3連複 (1・2・3列目 統合BOX) ---
    box_cars_3 = list(set(c1_list + c2_list + c3_list))
    sanrenpuku = []
    if len(box_cars_3) >= 3:
      for i in range(len(box_cars_3)):
        for j in range(i + 1, len(box_cars_3)):
          for k in range(j + 1, len(box_cars_3)):
            combo = sorted([box_cars_3[i], box_cars_3[j], box_cars_3[k]])
            sanrenpuku.append(f"{combo[0]}={combo[1]}={combo[2]}")
    eval_pattern(
        "3連複", "1〜3列目 全車番BOX", set(sanrenpuku), payout_dict, results
    )

  report = []
  for (t_type, p_name), data in results.items():
    inv = data["investment"]
    ret = data["return"]
    roi = (ret / inv * 100) if inv > 0 else 0
    hit_rate = (data["hits"] / len(df) * 100) if len(df) > 0 else 0

    report.append({
        "賭け式": t_type,
        "買い目パターン": p_name,
        "対象レース数": len(df),
        "総投資額": f"{inv:,} 円",
        "総払戻額": f"{ret:,} 円",
        "損益": f"{ret - inv:,} 円",
        "的中率": f"{hit_rate:.1f}%",
        "回収率 (ROI)": round(roi, 2),
    })

  summary_df = (
      pd.DataFrame(report)
      .sort_values(by="回収率 (ROI)", ascending=False)
      .reset_index(drop=True)
  )
  return summary_df, None


def eval_pattern(t_type, p_name, combos, payout_dict, results):
  key = (t_type, p_name)
  if key not in results:
    results[key] = {"investment": 0, "return": 0, "hits": 0}

  results[key]["investment"] += len(combos) * 100
  is_hit = False
  for combo in combos:
    if (t_type, combo) in payout_dict:
      results[key]["return"] += payout_dict[(t_type, combo)]
      is_hit = True

  if is_hit:
    results[key]["hits"] += 1