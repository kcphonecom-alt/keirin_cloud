from datetime import datetime, timedelta
import itertools
import json
import os
import re
import urllib.request
import pandas as pd
import streamlit as st

# psycopg2 の安全なインポート
try:
  import psycopg2
except ImportError:
  try:
    import psycopg2_binary as psycopg2
  except ImportError:
    psycopg2 = None

# --- ページ基本設定 ---
st.set_page_config(
    page_title="競輪 高回収率分析＆ミッドナイトプラットフォーム",
    layout="wide",
)

st.markdown(
    """
    <style>
    .main .block-container {
        max-width: 95%;
        padding-top: 1rem;
        padding-bottom: 1rem;
    }
    html, body, [class*="css"] {
        -webkit-font-smoothing: antialiased;
        text-rendering: optimizeLegibility;
        font-size: 14px !important;
    }
    </style>
""",
    unsafe_allow_html=True,
)


def get_db_connection():
  DATABASE_URL = os.environ.get(
      "DATABASE_URL",
      "postgresql://postgres.rtlnseqyepazjhyhsvpx:Qpqpqpqpqp123!#@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres",
  )
  if psycopg2 is None:
    raise ModuleNotFoundError("psycopg2-binary パッケージが見つかりません。")
  return psycopg2.connect(DATABASE_URL)


@st.cache_data(ttl=60, show_spinner=False)
def load_replica_data():
  try:
    conn = get_db_connection()
    df_players = pd.read_sql("SELECT * FROM player_stats", conn)
    df_memos = pd.read_sql("SELECT * FROM race_memos", conn)
    df_results = pd.read_sql("SELECT * FROM race_results", conn)
    conn.close()
    return df_players, df_memos, df_results
  except Exception:
    return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()


def init_db():
  try:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
            CREATE TABLE IF NOT EXISTS race_memos (
                "レースID" TEXT PRIMARY KEY, "PRO予想テキスト" TEXT, "構造化データJSON" TEXT,
                "発走時間" TEXT
            )
        """)
    conn.commit()
    conn.close()
  except Exception:
    pass


init_db()


# --- 完全精査版パーサー：BOX・イコール・フォーメーションの完全自動展開エンジン ---
def parse_bet_string_detailed_rows(
    bet_str, r1, r2, r3, pay_3t, pay_3f, pay_2t, payout_json_raw
):
  bets_3t = set()
  bets_3f = set()
  bets_2t = set()
  bets_w = set()

  official_pay_3t = pay_3t
  official_pay_3f = pay_3f
  official_pay_2t = pay_2t
  wide_dict = {}

  if payout_json_raw:
    try:
      p_list = (
          json.loads(payout_json_raw)
          if isinstance(payout_json_raw, str)
          else payout_json_raw
      )
      if isinstance(p_list, list):
        for item in p_list:
          b_type = item.get("賭け式")
          b_組番 = str(item.get("組番", ""))
          b_amt_str = str(item.get("払戻金", ""))
          b_amt = int(re.sub(r"\D", "", b_amt_str)) if b_amt_str else 0

          if b_type == "3連単" and official_pay_3t == 0:
            official_pay_3t = b_amt
          elif b_type == "3連複" and official_pay_3f == 0:
            official_pay_3f = b_amt
          elif b_type == "2車単" and official_pay_2t == 0:
            official_pay_2t = b_amt
          elif b_type == "ワイド":
            cars = [int(c) for c in re.findall(r"\d+", b_組番)]
            if len(cars) >= 2:
              pair = tuple(sorted([cars[0], cars[1]]))
              wide_dict[pair] = b_amt
    except Exception:
      pass

  if bet_str and isinstance(bet_str, str):
    normalized = bet_str.replace("ー", "-").replace("＝", "=").replace(" ", "")
    raw_lines = re.split(r"[\n,、;]", normalized)

    for line in raw_lines:
      s = line.strip()
      if not s:
        continue
      parts = re.split(r"[/]", s)
      for p in parts:
        p = p.strip()
        if not p:
          continue

        if "=" in p and "-" not in p:
          cars = [int(c) for c in re.findall(r"\d+", p)]
          cars = list(dict.fromkeys(cars))
          if len(cars) >= 3:
            for f, sec, t in itertools.permutations(cars[:3], 3):
              bets_3t.add((f, sec, t))
            for comb in itertools.combinations(cars[:3], 3):
              bets_3f.add(tuple(sorted(comb)))
            for f, sec in itertools.permutations(cars[:3], 2):
              bets_2t.add((f, sec))
            for w_comb in itertools.combinations(cars[:3], 2):
              bets_w.add(tuple(sorted(w_comb)))
            continue
          elif len(cars) == 2:
            bets_2t.add((cars[0], cars[1]))
            bets_2t.add((cars[1], cars[0]))
            bets_w.add(tuple(sorted(cars)))
            continue

        m_complex = re.match(r"^([\d]+)-(\d+)=([\d]+)$", p)
        if m_complex:
          first_group = [int(c) for c in m_complex.group(1)]
          sec_group = [int(c) for c in m_complex.group(2)]
          third_group = [int(c) for c in m_complex.group(3)]

          for f in first_group:
            for s_car in sec_group:
              for t in third_group:
                if len({f, s_car, t}) == 3:
                  bets_3t.add((f, s_car, t))
                  bets_3f.add(tuple(sorted([f, s_car, t])))
                if len({f, t, s_car}) == 3:
                  bets_3t.add((f, t, s_car))
                  bets_3f.add(tuple(sorted([f, t, s_car])))
                if f != s_car:
                  bets_2t.add((f, s_car))
                  bets_w.add(tuple(sorted([f, s_car])))
                if f != t:
                  bets_2t.add((f, t))
                  bets_w.add(tuple(sorted([f, t])))
                if s_car != t:
                  bets_2t.add((s_car, t))
                  bets_w.add(tuple(sorted([s_car, t])))
          continue

        m_axis = re.match(r"^(\d+)-(\d+)=([\d]+)$", p)
        if m_axis:
          first_car = int(m_axis.group(1))
          sec_car = int(m_axis.group(2))
          opponents = [int(c) for c in m_axis.group(3)]

          for opp in opponents:
            if len({first_car, sec_car, opp}) == 3:
              bets_3t.add((first_car, sec_car, opp))
              bets_3t.add((first_car, opp, sec_car))
              bets_3f.add(tuple(sorted([first_car, sec_car, opp])))
              bets_2t.add((first_car, sec_car))
              bets_2t.add((first_car, opp))
              bets_w.add(tuple(sorted([first_car, sec_car])))
              bets_w.add(tuple(sorted([first_car, opp])))
              bets_w.add(tuple(sorted([sec_car, opp])))
          continue

        p_hyphen_eq = p.replace("=", "-")
        tokens = p_hyphen_eq.split("-")

        if len(tokens) == 3:
          g1 = [int(c) for c in tokens[0] if c.isdigit()]
          g2 = [int(c) for c in tokens[1] if c.isdigit()]
          g3 = [int(c) for c in tokens[2] if c.isdigit()]

          for f in g1:
            for sec in g2:
              for t in g3:
                if len({f, sec, t}) == 3:
                  bets_3t.add((f, sec, t))
                  bets_3f.add(tuple(sorted([f, sec, t])))
                  bets_2t.add((f, sec))
                  bets_w.add(tuple(sorted([f, sec])))
          continue

        if len(tokens) == 2:
          g1 = [int(c) for c in tokens[0] if c.isdigit()]
          g2 = [int(c) for c in tokens[1] if c.isdigit()]

          for f in g1:
            for sec in g2:
              if f != sec:
                bets_2t.add((f, sec))
                bets_w.add(tuple(sorted([f, sec])))
          continue

  has_result = pd.notnull(r1) and pd.notnull(r2) and pd.notnull(r3)
  real_3t = (int(r1), int(r2), int(r3)) if has_result else None
  real_3f = tuple(sorted([int(r1), int(r2), int(r3)])) if has_result else None
  real_2t = (int(r1), int(r2)) if has_result else None

  rows = []
  p_3t_got, p_3f_got, p_2t_got, p_w_got = 0, 0, 0, 0

  for b in sorted(bets_3t):
    b_str = f"{b[0]}-{b[1]}-{b[2]}"
    is_hit = has_result and (b == real_3t)
    got = official_pay_3t if is_hit else 0
    p_3t_got += got
    rows.append({
        "券種": "3連単",
        "買い目": b_str,
        "判定": "🎯 的中" if is_hit else "ハズレ",
        "払戻金額": f"{got:,}円" if got > 0 else "-",
        "公式払戻": "",
    })

  for b in sorted(bets_3f):
    b_str = f"{b[0]}={b[1]}={b[2]}"
    is_hit = has_result and (b == real_3f)
    got = official_pay_3f if is_hit else 0
    p_3f_got += got
    rows.append({
        "券種": "3連複",
        "買い目": b_str,
        "判定": "🎯 的中" if is_hit else "ハズレ",
        "払戻金額": f"{got:,}円" if got > 0 else "-",
        "公式払戻": "",
    })

  for b in sorted(bets_2t):
    b_str = f"{b[0]}-{b[1]}"
    is_hit = has_result and (b == real_2t)
    got = official_pay_2t if is_hit else 0
    p_2t_got += got
    rows.append({
        "券種": "2車単",
        "買い目": b_str,
        "判定": "🎯 的中" if is_hit else "ハズレ",
        "払戻金額": f"{got:,}円" if got > 0 else "-",
        "公式払戻": "",
    })

  for b in sorted(bets_w):
    b_str = f"{b[0]}={b[1]}"
    is_hit = False
    got = 0
    if has_result and b in wide_dict:
      is_hit = True
      got = wide_dict[b]
    p_w_got += got
    rows.append({
        "券種": "ワイド",
        "買い目": b_str,
        "判定": "🎯 的中" if is_hit else "ハズレ",
        "払戻金額": f"{got:,}円" if got > 0 else "-",
        "公式払戻": "",
    })

  total_pts = len(bets_3t) + len(bets_3f) + len(bets_2t) + len(bets_w)
  total_cost = max(1, total_pts) * 100
  total_got = p_3t_got + p_3f_got + p_2t_got + p_w_got
  rec_rate = (total_got / total_cost * 100) if total_cost > 0 else 0

  unique_expanded_bets_list = [r["買い目"] for r in rows]

  return (
      rows,
      total_pts,
      total_cost,
      total_got,
      rec_rate,
      p_3t_got,
      p_3f_got,
      p_2t_got,
      p_w_got,
      unique_expanded_bets_list,
  )


# --- サイドバーメニュー ---
st.sidebar.title("🚴‍♂️ 競輪 分析プラットフォーム")
if st.sidebar.button("🔄 クラウドからデータを再同期"):
  st.cache_data.clear()
  st.success("✅ キャッシュをクリアしました！")
  st.rerun()

app_mode = st.sidebar.radio(
    "利用する機能を選択してください",
    [
        "📈 PRO予想家別 回収率アナリティクス (全券種分解)",
        "👤 PRO予想家 深掘り個別アナリティクス",
        "🌙 ミッドナイト高回収率 ＆ PRO予想融合シミュレータ",
        "🤖 ミッドナイトAI自動探索 (黄金比率の発見)",
        "PRO予想 1×2列目MIX分析",
    ],
)


# 共通データ前処理
def get_parsed_df():
  _, df_memos, df_results = load_replica_data()
  if df_memos.empty or df_results.empty:
    return pd.DataFrame()

  if "レースID" in df_results.columns:
    res_sub = df_results[[
        "レースID",
        "1着車番",
        "1着選手名",
        "2着車番",
        "2着選手名",
        "3着車番",
        "3着選手名",
        "払戻金JSON",
    ]].copy()
    for col in [
        "1着車番",
        "1着選手名",
        "2着車番",
        "2着選手名",
        "3着車番",
        "3着選手名",
        "払戻金JSON",
    ]:
      if col in df_memos.columns:
        df_memos = df_memos.drop(columns=[col])
    df_memos = pd.merge(df_memos, res_sub, on="レースID", how="left")

  parsed_records = []
  for _, row in df_memos.iterrows():
    race_id = str(row["レースID"])
    date_str = "-"
    venue_str = "-"
    try:
      parts = race_id.split("_")
      if len(parts) >= 2:
        date_str = parts[0]
        venue_str = parts[1]
    except:
      pass

    j_text = row.get("構造化データJSON")
    r1, r2, r3 = row.get("1着車番"), row.get("2着車番"), row.get("3着車番")
    payout_json_raw = row.get("払戻金JSON")

    has_result = pd.notnull(r1) and pd.notnull(r2) and pd.notnull(r3)
    result_str = f"{int(r1)}-{int(r2)}-{int(r3)}" if has_result else "未確定"

    p1 = row.get("Pro予想買い目1", "")
    p2 = row.get("Pro予想買い目2", "")
    p3 = row.get("Pro予想買い目3", "")

    if j_text:
      try:
        plist = json.loads(j_text)
        for p in plist:
          author = p.get("予想家", "不明")
          raw_bets = p.get("買い目", "")
          comment = p.get("コメント", "")

          (
              rows_data,
              t_pts,
              t_cost,
              t_got,
              r_rate,
              p_3t_got,
              p_3f_got,
              p_2t_got,
              p_w_got,
              expanded_bets_list,
          ) = parse_bet_string_detailed_rows(
              raw_bets, r1, r2, r3, 0, 0, 0, payout_json_raw
          )

          parsed_records.append({
              "レースID": race_id,
              "開催日": date_str,
              "開催会場": venue_str,
              "発走時間": row.get("発走時間", "-"),
              "予想家": author,
              "買い目原文": raw_bets,
              "Pro予想買い目1": p1,
              "Pro予想買い目2": p2,
              "Pro予想買い目3": p3,
              "展開買い目リスト": expanded_bets_list,
              "コメント": comment,
              "結果着順": result_str,
              "総点数": t_pts,
              "投資額": t_cost,
              "払戻合計": t_got,
              "3連単払戻": p_3t_got,
              "3連複払戻": p_3f_got,
              "2車単払戻": p_2t_got,
              "ワイド払戻": p_w_got,
              "回収率": r_rate,
              "sheet_rows": rows_data,
          })
      except Exception:
        pass
  return pd.DataFrame(parsed_records)


# ==========================================
# 1. PRO予想家別 回収率アナリティクス
# ==========================================
if app_mode == "📈 PRO予想家別 回収率アナリティクス (全券種分解)":
  st.title("📈 PRO予想家別 スプレッドシート風個別買い目詳細アナリティクス")
  df_parsed = get_parsed_df()

  if not df_parsed.empty:
    st.markdown("### 🔍 表示フィルター設定")
    f_col1, f_col2 = st.columns(2)

    all_dates = sorted(df_parsed["開催日"].unique().tolist(), reverse=True)
    selected_dates = f_col1.multiselect(
        "📅 開催日で絞り込み (未選択時は全期間)", all_dates
    )

    all_venues = sorted(df_parsed["開催会場"].unique().tolist())
    selected_venues = f_col2.multiselect(
        "🏟️ 開催会場で絞り込み (未選択時は全会場)", all_venues
    )

    df_filtered = df_parsed.copy()
    if selected_dates:
      df_filtered = df_filtered[df_filtered["開催日"].isin(selected_dates)]
    if selected_venues:
      df_filtered = df_filtered[
          df_filtered["開催会場"].isin(selected_venues)
      ]

    if df_filtered.empty:
      st.warning(
          "選択された条件に一致するレースデータが見つかりませんでした。"
      )
    else:
      summary_rows = []
      for auth, group in df_filtered.groupby("予想家"):
        tot_races = len(group)
        tot_pts = group["総点数"].sum()
        tot_cost = group["投資額"].sum()

        sum_3t = group["3連単払戻"].sum()
        sum_3f = group["3連複払戻"].sum() if "3連複払戻" in group else 0
        sum_2t = group["2車単払戻"].sum()
        sum_w = group["ワイド払戻"].sum()
        sum_all = group["払戻合計"].sum()

        rec_3t = (sum_3t / tot_cost * 100) if tot_cost > 0 else 0
        rec_3f = (sum_3f / tot_cost * 100) if tot_cost > 0 else 0
        rec_2t = (sum_2t / tot_cost * 100) if tot_cost > 0 else 0
        rec_w = (sum_w / tot_cost * 100) if tot_cost > 0 else 0
        rec_total = (sum_all / tot_cost * 100) if tot_cost > 0 else 0

        summary_rows.append({
            "予想家": auth,
            "予想レース数": tot_races,
            "総点数": f"{tot_pts:,}点",
            "総投資額": f"{tot_cost:,}円",
            "3連単払戻 (回収率)": f"{sum_3t:,}円 ({rec_3t:.1f}%)",
            "3連複払戻 (回収率)": f"{sum_3f:,}円 ({rec_3f:.1f}%)",
            "2車単払戻 (回収率)": f"{sum_2t:,}円 ({rec_2t:.1f}%)",
            "ワイド払戻 (回収率)": f"{sum_w:,}円 ({rec_w:.1f}%)",
            "総合計払戻金": f"{sum_all:,}円",
            "総合回収率": f"{rec_total:.1f}%",
        })

      df_summary_table = pd.DataFrame(summary_rows).sort_values(
          by="予想レース数", ascending=False
      )
      st.dataframe(df_summary_table, use_container_width=True, hide_index=True)

      st.markdown("---")
      st.subheader("📋 参加レースのシート風詳細ビュー (全レース・新しい順)")

      df_sorted_races = df_filtered.sort_values(
          by=["開催日", "レースID"], ascending=[False, False]
      )

      total_records = len(df_sorted_races)
      items_per_page = 20
      total_pages = max(1, (total_records + items_per_page - 1) // items_per_page)

      p_col1, p_col2 = st.columns([2, 3])
      p_col1.markdown(
          f"📊 **全 {total_records:,} 件** のレースが見つかりました。"
      )
      selected_page = p_col2.number_input(
          f"ページ選択 (全 {total_pages} ページ)",
          min_value=1,
          max_value=total_pages,
          value=1,
          step=1,
      )

      start_idx = (selected_page - 1) * items_per_page
      end_idx = start_idx + items_per_page
      df_current_page = df_sorted_races.iloc[start_idx:end_idx]

      for _, r in df_current_page.iterrows():
        with st.container():
          status_badge = "🎯 的中" if r["払戻合計"] > 0 else "❌ ハズレ"
          st.markdown(
              f"##### 🏇 **{r['レースID']}** (発走: {r['発走時間']}) ｜ 予想家:"
              f" **{r['予想家']}** ｜ 結果着順: `{r['結果着順']}` ｜ 判定:"
              f" **{status_badge}**"
          )

          c_info1, c_info2 = st.columns([1, 2])
          c_info1.markdown(f"**🎯 買い目原文:** `{r['買い目原文']}`")
          c_info2.markdown(f"**💬 コメント:** {r['コメント']}")

          df_sheet = pd.DataFrame(r["sheet_rows"])
          st.dataframe(df_sheet, use_container_width=True, hide_index=True)

          st.markdown(
              f"📊 **【トータル集計】** 総点数: **{r['総点数']}点** ｜ 投資:"
              f" **{r['投資額']:,}円** ｜ 払戻: **{r['払戻合計']:,}円** ｜ 回収率:"
              f" **{r['回収率']:.1f}%**"
          )
          st.markdown("---")
  else:
    st.warning("有効なデータが見つかりませんでした。")

# ==========================================
# 2. PRO予想家 深掘り個別アナリティクス
# ==========================================
elif app_mode == "👤 PRO予想家 深掘り個別アナリティクス":
  st.title("👤 PRO予想家 深掘り個別アナリティクスルーム")
  st.markdown(
      "特定の予想家を一人ピックアップし、その予想家のパフォーマンスを徹底的に分析します。"
  )

  df_parsed = get_parsed_df()

  if not df_parsed.empty:
    author_list = sorted(df_parsed["予想家"].unique().tolist())
    target_author = st.selectbox(
        "🔍 分析したいPRO予想家を選択してください", author_list
    )

    df_auth = df_parsed[df_parsed["予想家"] == target_author].copy()

    if not df_auth.empty:
      tot_races = len(df_auth)
      tot_cost = df_auth["投資額"].sum()
      tot_got = df_auth["払戻合計"].sum()
      overall_rec = (tot_got / tot_cost * 100) if tot_cost > 0 else 0
      hit_races = len(df_auth[df_auth["払戻合計"] > 0])
      hit_rate = (hit_races / tot_races * 100) if tot_races > 0 else 0

      st.markdown(f"### 📊 【 {target_author} 】の総合パフォーマンス")
      col1, col2, col3, col4, col5 = st.columns(5)
      col1.metric("予想レース数", f"{tot_races:,} レース")
      col2.metric("的中レース数", f"{hit_races:,} レース ({hit_rate:.1f}%)")
      col3.metric("総投資額", f"{tot_cost:,} 円")
      col4.metric("総払戻金", f"{tot_got:,} 円")
      col5.metric(
          "総合回収率",
          f"{overall_rec:.1f}%",
          delta=f"{overall_rec - 100:.1f}% vs 100%",
      )

      st.markdown("---")

      st.subheader("🎯 券種別 獲得払戻 ＆ 回収率内訳")
      sum_3t = df_auth["3連単払戻"].sum()
      sum_3f = df_auth["3連複払戻"].sum() if "3連複払戻" in df_auth else 0
      sum_2t = df_auth["2車単払戻"].sum()
      sum_w = df_auth["ワイド払戻"].sum()

      rc_3t = (sum_3t / tot_cost * 100) if tot_cost > 0 else 0
      rc_3f = (sum_3f / tot_cost * 100) if tot_cost > 0 else 0
      rc_2t = (sum_2t / tot_cost * 100) if tot_cost > 0 else 0
      rc_w = (sum_w / tot_cost * 100) if tot_cost > 0 else 0

      bcol1, bcol2, bcol3, bcol4 = st.columns(4)
      bcol1.metric(
          "3連単 回収率", f"{rc_3t:.1f}%", f"{sum_3t:,}円払戻", delta_color="normal"
      )
      bcol2.metric(
          "3連複 回収率", f"{rc_3f:.1f}%", f"{sum_3f:,}円払戻", delta_color="normal"
      )
      bcol3.metric(
          "2車単 回収率", f"{rc_2t:.1f}%", f"{sum_2t:,}円払戻", delta_color="normal"
      )
      bcol4.metric(
          "ワイド 回収率", f"{rc_w:.1f}%", f"{sum_w:,}円払戻", delta_color="normal"
      )

      st.markdown("---")
      st.subheader(f"📋 {target_author} の全予想履歴一覧")

      df_display = df_auth[[
          "レースID",
          "発走時間",
          "結果着順",
          "買い目原文",
          "総点数",
          "投資額",
          "払戻合計",
          "回収率",
      ]].sort_values(by="レースID", ascending=False)

      st.dataframe(
          df_display,
          column_config={
              "投資額": st.column_config.NumberColumn("投資額", format="%d 円"),
              "払戻合計": st.column_config.NumberColumn("払戻合計", format="%d 円"),
              "回収率": st.column_config.NumberColumn("回収率", format="%.1f %%"),
          },
          use_container_width=True,
          hide_index=True,
      )

      st.markdown("---")

      st.subheader("🔍 レースごとの【展開買い目・判定詳細（1列表示）】")

      race_list = df_auth["レースID"].tolist()
      selected_race_id = st.selectbox(
          "詳細を確認したいレースIDを選択してください", race_list
      )

      race_row = df_auth[df_auth["レースID"] == selected_race_id].iloc[0]

      st.markdown(
          f"🏇 レースID: `{selected_race_id}` ｜ 買い目原文:"
          f" `{race_row['買い目原文']}` ｜ 総点数: `{race_row['総点数']}点` ｜ 結果:"
          f" `{race_row['結果着順']}`"
      )

      df_details = pd.DataFrame(race_row["sheet_rows"])
      if not df_details.empty:
        st.dataframe(
            df_details[["券種", "買い目", "判定", "払戻金額"]],
            use_container_width=True,
            hide_index=True,
        )
      else:
        st.info("展開データがありません。")

    else:
      st.info(f"{target_author} のデータが見つかりませんでした。")
  else:
    st.warning("データがありません。")

# ==========================================
# 3. ミッドナイト高回収率 ＆ PRO予想融合シミュレータ
# ==========================================
elif app_mode == "🌙 ミッドナイト高回収率 ＆ PRO予想融合シミュレータ":
  st.title("🌙 ミッドナイト特化型 回収率＆根拠レース探索シミュレータ")
  st.markdown("Supabaseの正式なレース結果（`race_results`）を基準に、発走20:35以降のミッドナイトレースにおける全券種（3連単・3連複・2車単・ワイド）の圧倒的な的中状況と回収率を徹底検証します。")

  _, _, df_results = load_replica_data()

  if not df_results.empty:
    st.markdown("---")

    strategy_mode = st.selectbox(
        "🎯 検証する買い目の選択方式",
        [
            "固定BOX: 1-2-3 BOX (3連単6点 / 3連複1点など)",
            "固定BOX: 1-2-3-7 BOX (4車BOX)",
            "固定BOX: 1-2-3-6 BOX (4車BOX)",
            "カスタム自由入力 (1・2・3列目を指定)",
        ],
    )

    bet_unit = st.number_input(
        "1点あたりの投資金額（円）",
        min_value=100,
        max_value=10000,
        value=100,
        step=100,
    )

    target_bets_3t = set()
    target_boxes_3f = set()
    target_bets_2t = set()
    target_boxes_w = set()

    if "1-2-3 BOX" in strategy_mode:
      cars = [1, 2, 3]
      for a, b, c in itertools.permutations(cars, 3):
        target_bets_3t.add((a, b, c))
      for comb in itertools.combinations(cars, 3):
        target_boxes_3f.add(tuple(sorted(comb)))
      for a, b in itertools.permutations(cars, 2):
        target_bets_2t.add((a, b))
      for w_comb in itertools.combinations(cars, 2):
        target_boxes_w.add(tuple(sorted(w_comb)))
      st.info("📌 **対象BOX**: 1, 2, 3 車番の組み合わせ")

    elif "1-2-3-7 BOX" in strategy_mode:
      cars = [1, 2, 3, 7]
      for a, b, c in itertools.permutations(cars, 3):
        target_bets_3t.add((a, b, c))
      for comb in itertools.combinations(cars, 3):
        target_boxes_3f.add(tuple(sorted(comb)))
      for a, b in itertools.permutations(cars, 2):
        target_bets_2t.add((a, b))
      for w_comb in itertools.combinations(cars, 2):
        target_boxes_w.add(tuple(sorted(w_comb)))
      st.info("📌 **対象BOX**: 1, 2, 3, 7 車番の4車BOX")

    elif "1-2-3-6 BOX" in strategy_mode:
      cars = [1, 2, 3, 6]
      for a, b, c in itertools.permutations(cars, 3):
        target_bets_3t.add((a, b, c))
      for comb in itertools.combinations(cars, 3):
        target_boxes_3f.add(tuple(sorted(comb)))
      for a, b in itertools.permutations(cars, 2):
        target_bets_2t.add((a, b))
      for w_comb in itertools.combinations(cars, 2):
        target_boxes_w.add(tuple(sorted(w_comb)))
      st.info("📌 **対象BOX**: 1, 2, 3, 6 車番の4車BOX")

    else:
      st.markdown("#### 🛠️ カスタム自由入力")
      c_in1, c_in2, c_in3 = st.columns(3)
      with c_in1:
        g1_in = st.text_input("1着候補", "1")
      with c_in2:
        g2_in = st.text_input("2着候補", "1,2,3")
      with c_in3:
        g3_in = st.text_input("3着候補", "1,2,3,5,7")

      try:
        f_list = [
            int(x.strip()) for x in g1_in.split(",") if x.strip().isdigit()
        ]
        s_list = [
            int(x.strip()) for x in g2_in.split(",") if x.strip().isdigit()
        ]
        t_list = [
            int(x.strip()) for x in g3_in.split(",") if x.strip().isdigit()
        ]

        for f in f_list:
          for s in s_list:
            for t in t_list:
              if len({f, s, t}) == 3:
                target_bets_3t.add((f, s, t))
                target_boxes_3f.add(tuple(sorted([f, s, t])))
                target_bets_2t.add((f, s))
                target_boxes_w.add(tuple(sorted([f, s])))
                target_boxes_w.add(tuple(sorted([s, t])))
                target_boxes_w.add(tuple(sorted([f, t])))
      except Exception:
        pass
      st.success(f"📌 **カスタム構築点数**: 3連単 {len(target_bets_3t)} 点")

    st.markdown("---")

    if st.button(
        "🌙 Supabase結果データと突き合わせてバックテストを実行", type="primary"
    ):
      midnight_races = 0
      total_investment = 0

      # 券種別の的中数・払戻金カウンター
      payout_3t_total = 0
      payout_3f_total = 0
      payout_2t_total = 0
      payout_w12_total = 0
      payout_w13_total = 0
      payout_w23_total = 0

      hit_3t_count = 0
      hit_3f_count = 0
      hit_2t_count = 0
      hit_w12_count = 0
      hit_w13_count = 0
      hit_w23_count = 0

      race_summary_rows = []

      # 買い目の点数を各券種ごとに算出
      cost_3t = len(target_bets_3t) * bet_unit
      cost_3f = len(target_boxes_3f) * bet_unit
      cost_2t = len(target_bets_2t) * bet_unit
      cost_w = len(target_boxes_w) * bet_unit

      total_cost_per_race = cost_3t + cost_3f + cost_2t + cost_w

      for _, row in df_results.iterrows():
        race_id = row.get("レースID", "")
        time_str = str(row.get("発走時間", "21:00"))

        is_midnight = True
        if time_str and ":" in time_str:
          try:
            parts = time_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1])
            if not (hour > 20 or (hour == 20 and minute >= 35) or hour < 4):
              is_midnight = False
          except:
            pass

        if not is_midnight:
          continue

        midnight_races += 1
        r1 = row.get("1着車番") or row.get("1st_car")
        r2 = row.get("2着車番") or row.get("2nd_car")
        r3 = row.get("3着車番") or row.get("3rd_car")

        has_result = pd.notnull(r1) and pd.notnull(r2) and pd.notnull(r3)
        total_investment += total_cost_per_race

        hit_3t_str = "-"
        hit_3f_str = "-"
        hit_2t_str = "-"
        hit_w12_str = "-"
        hit_w13_str = "-"
        hit_w23_str = "-"

        r_payout_sum = 0

        if has_result:
          try:
            real_1, real_2, real_3 = int(r1), int(r2), int(r3)
            real_3t = (real_1, real_2, real_3)
            real_3f = tuple(sorted([real_1, real_2, real_3]))
            real_2t = (real_1, real_2)

            box_3cars = sorted([real_1, real_2, real_3])
            w_pair_12 = tuple(sorted([box_3cars[0], box_3cars[1]]))
            w_pair_13 = tuple(sorted([box_3cars[0], box_3cars[2]]))
            w_pair_23 = tuple(sorted([box_3cars[1], box_3cars[2]]))

            payout_json_raw = row.get("払戻金JSON")
            p_list = (
                json.loads(payout_json_raw)
                if isinstance(payout_json_raw, str)
                else payout_json_raw
            )

            official_3t = 0
            official_3f = 0
            official_2t = 0
            wide_dict = {}

            if isinstance(p_list, list):
              for item in p_list:
                b_type = item.get("賭け式")
                b_comb = str(item.get("組番", ""))
                b_amt_str = str(item.get("払戻金", "0"))
                b_amt = int(re.sub(r"\D", "", b_amt_str)) if b_amt_str else 0

                if b_type == "3連単":
                  official_3t = b_amt
                elif b_type == "3連複":
                  official_3f = b_amt
                elif b_type == "2車単":
                  official_2t = b_amt
                elif b_type == "ワイド":
                  cars = [int(c) for c in re.findall(r"\d+", b_comb)]
                  if len(cars) >= 2:
                    wide_dict[tuple(sorted([cars[0], cars[1]]))] = b_amt

            # 3連単判定
            if real_3t in target_bets_3t:
              earned = official_3t * (bet_unit / 100)
              payout_3t_total += earned
              r_payout_sum += earned
              hit_3t_count += 1
              hit_3t_str = f"🎯 的中 ({int(earned):,}円)"

            # 3連複判定
            if real_3f in target_boxes_3f:
              earned = official_3f * (bet_unit / 100)
              payout_3f_total += earned
              r_payout_sum += earned
              hit_3f_count += 1
              hit_3f_str = f"🎯 的中 ({int(earned):,}円)"

            # 2車単判定
            if real_2t in target_bets_2t:
              earned = official_2t * (bet_unit / 100)
              payout_2t_total += earned
              r_payout_sum += earned
              hit_2t_count += 1
              hit_2t_str = f"🎯 的中 ({int(earned):,}円)"

            # ワイド(1-2)判定
            if w_pair_12 in target_boxes_w and w_pair_12 in wide_dict:
              earned = wide_dict[w_pair_12] * (bet_unit / 100)
              payout_w12_total += earned
              r_payout_sum += earned
              hit_w12_count += 1
              hit_w12_str = f"🎯 的中 ({int(earned):,}円)"

            # ワイド(1-3)判定
            if w_pair_13 in target_boxes_w and w_pair_13 in wide_dict:
              earned = wide_dict[w_pair_13] * (bet_unit / 100)
              payout_w13_total += earned
              r_payout_sum += earned
              hit_w13_count += 1
              hit_w13_str = f"🎯 的中 ({int(earned):,}円)"

            # ワイド(2-3)判定
            if w_pair_23 in target_boxes_w and w_pair_23 in wide_dict:
              earned = wide_dict[w_pair_23] * (bet_unit / 100)
              payout_w23_total += earned
              r_payout_sum += earned
              hit_w23_count += 1
              hit_w23_str = f"🎯 的中 ({int(earned):,}円)"

          except Exception:
            pass

        result_display = (
            f"{int(r1)}-{int(r2)}-{int(r3)}" if has_result else "未確定"
        )
        race_summary_rows.append({
            "レースID": race_id,
            "発走時間": time_str,
            "結果": result_display,
            "3連単": hit_3t_str,
            "3連複": hit_3f_str,
            "2車単": hit_2t_str,
            "ワイド(1-2)": hit_w12_str,
            "ワイド(1-3)": hit_w13_str,
            "ワイド(2-3)": hit_w23_str,
            "レース総払戻": int(r_payout_sum),
        })

      # --- 📊 サマリー表示の拡張 ---
      st.subheader("📊 ミッドナイト全券種 集計結果サマリー")

      total_payout_all = (
          payout_3t_total
          + payout_3f_total
          + payout_2t_total
          + payout_w12_total
          + payout_w13_total
          + payout_w23_total
      )
      overall_rec = (
          (total_payout_all / total_investment * 100)
          if total_investment > 0
          else 0
      )

      # 総合メトリック
      c1, c2, c3, c4, c5 = st.columns(5)
      c1.metric("検証レース数", f"{midnight_races:,} レース")
      c2.metric(
          "3連単 的中数",
          f"{hit_3t_count:,} レース"
          f" ({hit_3t_count/midnight_races*100:.1f}%)"
          if midnight_races > 0
          else "0",
      )
      c3.metric("総投資額", f"{total_investment:,} 円")
      c4.metric("総払戻金", f"{int(total_payout_all):,} 円")
      c5.metric(
          "総合回収率",
          f"{overall_rec:.1f}%",
          delta=f"{overall_rec - 100:.1f}% vs 100%",
      )

      st.markdown("##### 🎟️ 各券種別の個別パフォーマンス詳細")
      t_col1, t_col2, t_col3, t_col4, t_col5, t_col6 = st.columns(6)

      # 3連単
      roi_3t = (
          (payout_3t_total / (cost_3t * midnight_races) * 100)
          if (cost_3t * midnight_races) > 0
          else 0
      )
      t_col1.metric(
          "3連単",
          f"{hit_3t_count:,}回 ({hit_3t_count/midnight_races*100:.1f}%)"
          if midnight_races > 0
          else "0",
          f"回収率: {roi_3t:.1f}%",
      )

      # 3連複
      roi_3f = (
          (payout_3f_total / (cost_3f * midnight_races) * 100)
          if (cost_3f * midnight_races) > 0
          else 0
      )
      t_col2.metric(
          "3連複",
          f"{hit_3f_count:,}回 ({hit_3f_count/midnight_races*100:.1f}%)"
          if midnight_races > 0
          else "0",
          f"回収率: {roi_3f:.1f}%",
      )

      # 2車単
      roi_2t = (
          (payout_2t_total / (cost_2t * midnight_races) * 100)
          if (cost_2t * midnight_races) > 0
          else 0
      )
      t_col3.metric(
          "2車単",
          f"{hit_2t_count:,}回 ({hit_2t_count/midnight_races*100:.1f}%)"
          if midnight_races > 0
          else "0",
          f"回収率: {roi_2t:.1f}%",
      )

      # ワイド 1-2
      cost_w_single = (
          len(target_boxes_w) // 3 if len(target_boxes_w) > 0 else 1
      )
      tot_cost_w_single = max(1, cost_w_single) * bet_unit * midnight_races
      roi_w12 = (
          (payout_w12_total / tot_cost_w_single * 100)
          if tot_cost_w_single > 0
          else 0
      )
      t_col4.metric(
          "ワイド(1-2)",
          f"{hit_w12_count:,}回 ({hit_w12_count/midnight_races*100:.1f}%)"
          if midnight_races > 0
          else "0",
          f"回収率: {roi_w12:.1f}%",
      )

      # ワイド 1-3
      roi_w13 = (
          (payout_w13_total / tot_cost_w_single * 100)
          if tot_cost_w_single > 0
          else 0
      )
      t_col5.metric(
          "ワイド(1-3)",
          f"{hit_w13_count:,}回 ({hit_w13_count/midnight_races*100:.1f}%)"
          if midnight_races > 0
          else "0",
          f"回収率: {roi_w13:.1f}%",
      )

      # ワイド 2-3
      roi_w23 = (
          (payout_w23_total / tot_cost_w_single * 100)
          if tot_cost_w_single > 0
          else 0
      )
      t_col6.metric(
          "ワイド(2-3)",
          f"{hit_w23_count:,}回 ({hit_w23_count/midnight_races*100:.1f}%)"
          if midnight_races > 0
          else "0",
          f"回収率: {roi_w23:.1f}%",
      )

      st.markdown("---")
      st.subheader("📋 根拠となるレース＆全券種の的中状況リスト")
      df_res_table = pd.DataFrame(race_summary_rows)
      st.dataframe(df_res_table, use_container_width=True, hide_index=True)
  else:
    st.warning("Supabaseのレース結果データ（race_results）がありません。")

# ==========================================
# 4. ミッドナイトAI自動探索 (黄金比率の発見)
# ==========================================
elif app_mode == "🤖 ミッドナイトAI自動探索 (黄金比率の発見)":
  st.title("🤖 ミッドナイトAI自動探索 — 黄金比率の発見シミュレータ")
  st.markdown("発走20:35以降のミッドナイトレースを対象に自動探索を行います。")
  _, _, df_results = load_replica_data()
  if df_results.empty:
    st.warning("データがありません。")

# ==========================================
# 5. PRO予想 1×2列目MIX分析 (完全リニューアル版)
# ==========================================
elif app_mode == "PRO予想 1×2列目MIX分析":
  st.title("🎯 PRO予想 1列目×2列目 MIX回収率検証 & 勝ち筋発掘")
  st.markdown(
      "PRO予想の **「1列目（軸・1着候補）」** と **「2列目・3列目（対抗・相手候補）」**"
      " のデータを活用し、どの予想家の組み合わせが最も高い回収率（ROI）を叩き出しているかを完全に可視化・検証します。"
  )

  df_parsed = get_parsed_df()

  if not df_parsed.empty:
    st.markdown("---")
    st.subheader("🔍 分析パラメータ設定")

    s_col1, s_col2 = st.columns(2)
    target_ticket = s_col1.selectbox(
        "🎟️ 検証する賭け式を選択", ["3連単", "2車単", "3連複", "ワイド"]
    )
    author_filter = s_col2.multiselect(
        "👤 対象とするPRO予想家 (未選択時は全員対象)",
        sorted(df_parsed["予想家"].unique().tolist()),
    )

    df_target = df_parsed.copy()
    if author_filter:
      df_target = df_target[df_target["予想家"].isin(author_filter)]

    if st.button(
        "🚀 1列目×2列目 MIX分析シミュレーションを実行", type="primary"
    ):
      with st.spinner("データベースから各予想家の買い目と結果を照合中..."):
        mix_results = {}

        for _, row in df_target.iterrows():
          author = row["予想家"]
          if author not in mix_results:
            mix_results[author] = {
                "予想数": 0,
                "総投資": 0,
                "総払戻": 0,
                "的中数": 0,
            }

          mix_results[author]["予想数"] += 1
          mix_results[author]["総投資"] += row["投資額"]
          mix_results[author]["総払戻"] += row["払戻合計"]
          if row["払戻合計"] > 0:
            mix_results[author]["的中数"] += 1

        summary_list = []
        for auth, data in mix_results.items():
          inv = data["総投資"]
          ret = data["総払戻"]
          roi = (ret / inv * 100) if inv > 0 else 0
          hit_r = (data["的中数"] / data["予想数"] * 100) if data["予想数"] > 0 else 0

          summary_list.append({
              "PRO予想家": auth,
              "検証レース数": data["予想数"],
              "的中数": data["的中数"],
              "的中率": f"{hit_r:.1f}%",
              "総投資額": f"{inv:,} 円",
              "総払戻額": f"{ret:,} 円",
              "損益": f"{ret - inv:,} 円",
              "回収率 (ROI)": round(roi, 2),
          })

        if summary_list:
          df_res = pd.DataFrame(summary_list).sort_values(
              by="回収率 (ROI)", ascending=False
          )

          st.success("✨ シミュレーション分析が完了しました！")

          top_author = df_res.iloc[0]
          col1, col2, col3 = st.columns(3)
          col1.metric("🏆 最も勝てる推奨予想家", top_author["PRO予想家"])
          col2.metric("最高回収率 (ROI)", f"{top_author['回収率 (ROI)']}%")
          col3.metric("損益", top_author["損益"])

          st.markdown("---")
          st.subheader("📊 予想家別 1列目×2列目MIX 回収率ランキング")
          st.dataframe(df_res, use_container_width=True, hide_index=True)

          st.markdown("---")
          st.subheader("📋 【根拠詳細】選択した予想家の的中レース明細")
          selected_detail_author = st.selectbox(
              "詳細を確認する予想家を選択してください",
              df_res["PRO予想家"].tolist(),
          )

          df_author_races = df_target[
              (df_target["予想家"] == selected_detail_author)
              & (df_target["払戻合計"] > 0)
          ]
          if not df_author_races.empty:
            st.markdown(
                f"🎯 **{selected_detail_author}** の的中レース一覧（全"
                f" {len(df_author_races)} 件）"
            )
            st.dataframe(
                df_author_races[[
                    "レースID",
                    "発走時間",
                    "結果着順",
                    "買い目原文",
                    "投資額",
                    "払戻合計",
                    "回収率",
                ]],
                use_container_width=True,
                hide_index=True,
            )
          else:
            st.info(
                f"{selected_detail_author} の的中データが現在ありません。"
            )
        else:
          st.warning("集計可能なデータが見つかりませんでした。")
  else:
    st.warning("有効なデータが見つかりませんでした。")
