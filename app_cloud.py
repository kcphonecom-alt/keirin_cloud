from datetime import datetime, timedelta
import itertools
import json
import os
import re
import urllib.request
import pandas as pd
import streamlit as st

try:
  import ev_engine
except Exception:
  ev_engine = None

try:
  import ev_v2
except Exception:
  ev_v2 = None

try:
  import backtest_ev
except Exception:
  backtest_ev = None

try:
  import pro_trends
  if not hasattr(pro_trends, "type_stats"):
    # デプロイ直後は古いモジュールがメモリに残ることがあるため読み直す
    import importlib

    pro_trends = importlib.reload(pro_trends)
except Exception:
  pro_trends = None

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
    /* スマホ用カード表示の部品 */
    .m-card {
        border: 1px solid rgba(128,128,128,.35);
        border-radius: 10px;
        padding: 10px 12px;
        margin-bottom: 10px;
    }
    .m-head { font-weight: 700; font-size: 1.05rem; margin-bottom: 4px; word-break: break-all; }
    .m-row {
        display: flex; justify-content: space-between; gap: 12px;
        padding: 3px 0; border-top: 1px dashed rgba(128,128,128,.25);
    }
    .m-label { opacity: .75; flex-shrink: 0; }
    .m-val { font-weight: 600; text-align: right; word-break: break-all; }
    /* スマホ幅では表(pc_)を隠してカード(sp_)だけ出す */
    @media (max-width: 768px) {
        div[class*="st-key-pc_"] { display: none !important; }
        .main .block-container {
            max-width: 100%;
            padding-left: .7rem; padding-right: .7rem; padding-top: .5rem;
        }
        h1 { font-size: 1.45rem !important; line-height: 1.35 !important; }
        h2 { font-size: 1.25rem !important; }
        h3 { font-size: 1.1rem !important; }
        html, body, [class*="css"] { font-size: 13px !important; }
        [data-testid="stMetricValue"] { font-size: 1.35rem !important; }
        [data-testid="stMetricLabel"] { font-size: .8rem !important; }
    }
    /* PC幅ではカード(sp_)を隠して表(pc_)だけ出す */
    @media (min-width: 769px) {
        div[class*="st-key-sp_"] { display: none !important; }
    }
    </style>
""",
    unsafe_allow_html=True,
)


# --- 背景: ハンドル投げ画像を半透明で敷く（白グラデーションで薄く重ねる） ---
import base64 as _b64

_bg_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "handle_throw_bg.jpg"
)
try:
  with open(_bg_path, "rb") as _f:
    _bg_b64 = _b64.b64encode(_f.read()).decode()
  st.markdown(
      f"""
      <style>
      .stApp {{
          background-image:
              linear-gradient(rgba(255,255,255,0.84), rgba(255,255,255,0.93)),
              url("data:image/jpeg;base64,{_bg_b64}");
          background-size: cover;
          background-position: center top;
          background-repeat: no-repeat;
          background-attachment: fixed;
      }}
      </style>
      """,
      unsafe_allow_html=True,
  )
except Exception:
  pass


import html as _html_mod


def _fmt_cell(v):
  """カード表示用にセル値を読みやすい文字列へ整える。"""
  if v is None:
    return "―"
  try:
    if pd.isna(v):
      return "―"
  except Exception:
    pass
  if isinstance(v, bool):
    return "◎" if v else "―"
  if isinstance(v, int):
    return f"{v:,}"
  if isinstance(v, float):
    s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s if s else "0"
  return str(v)


def show_df_responsive(df, key, column_config=None, head_cols=1):
  """同じDataFrameを、PCでは表・スマホではカードで表示する（項目は省略しない）。

  st.container(key=...) が付ける st-key-pc_<key> / st-key-sp_<key> クラスを
  上のCSSメディアクエリで幅に応じて切り替える。
  """
  with st.container(key=f"pc_{key}"):
    if column_config:
      st.dataframe(
          df,
          column_config=column_config,
          use_container_width=True,
          hide_index=True,
      )
    else:
      st.dataframe(df, use_container_width=True, hide_index=True)
  with st.container(key=f"sp_{key}"):
    cols = list(df.columns)
    cards = []
    for _, row in df.iterrows():
      head = " ".join(_fmt_cell(row[c]) for c in cols[:head_cols])
      body = "".join(
          f'<div class="m-row"><span class="m-label">'
          f'{_html_mod.escape(str(c))}</span>'
          f'<span class="m-val">{_html_mod.escape(_fmt_cell(row[c]))}</span></div>'
          for c in cols[head_cols:]
      )
      cards.append(
          f'<div class="m-card"><div class="m-head">'
          f'{_html_mod.escape(head)}</div>{body}</div>'
      )
    st.markdown("".join(cards), unsafe_allow_html=True)



def get_db_connection():
  # ソースコード内からパスワードを削除し、環境変数（Streamlit Secrets）からのみ取得します
  DATABASE_URL = os.environ.get("DATABASE_URL")

  if not DATABASE_URL:
    raise ValueError("環境変数 'DATABASE_URL' が設定されていません。")

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


# --- 補助関数：ミッドナイト時間の判定（トップレベルに配置） ---
def is_midnight_time(time_str):
  if not time_str or ":" not in time_str:
    return False
  try:
    parts = time_str.split(":")
    hour = int(parts[0])
    minute = int(parts[1])
    return (hour > 20 or (hour == 20 and minute >= 35) or hour < 4)
  except:
    return False


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
        "🧮 ミッドナイト買い目 回収率チェッカー",
        "🎁 ミッドナイト特選 123BOX決着リスト",
        "🌙 ミッドナイト高回収率 ＆ PRO予想融合シミュレータ",
        "🤖 ミッドナイトAI自動探索 (黄金比率の発見)",
        "🚧工事中｜🎯 買い目別 EVランキング",
        "🎯 EV v2 確率モデル買い目算出",
        "🧪 EVバックテスト（実力測定）",
        "📊 PRO傾向＆会場別 勝ち筋分析",
        "🚧工事中｜📈 PRO予想家別 回収率アナリティクス (全券種分解)",
        "🚧工事中｜👤 PRO予想家 深掘り個別アナリティクス",
        "🚧工事中｜PRO予想 1×2列目MIX分析",
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
if app_mode == "🚧工事中｜📈 PRO予想家別 回収率アナリティクス (全券種分解)":
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
      show_df_responsive(df_summary_table, "ana_summary")

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

      for _ri, (_, r) in enumerate(df_current_page.iterrows()):
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
          show_df_responsive(df_sheet, f"ana_sheet_{start_idx + _ri}")

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
elif app_mode == "🚧工事中｜👤 PRO予想家 深掘り個別アナリティクス":
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

      show_df_responsive(
          df_display,
          "deep_list",
          column_config={
              "投資額": st.column_config.NumberColumn("投資額", format="%d 円"),
              "払戻合計": st.column_config.NumberColumn("払戻合計", format="%d 円"),
              "回収率": st.column_config.NumberColumn("回収率", format="%.1f %%"),
          },
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
        show_df_responsive(
            df_details[["券種", "買い目", "判定", "払戻金額"]], "deep_detail"
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
      show_df_responsive(df_res_table, "sim_table")
  else:
    st.warning("Supabaseのレース結果データ（race_results）がありません。")

# ==========================================
# 4. ミッドナイトAI自動探索 (黄金比率の発見)
# ==========================================
elif app_mode == "🤖 ミッドナイトAI自動探索 (黄金比率の発見)":
  st.title("🤖 ミッドナイトAI自動探索 — 黄金比率の発見シミュレータ")
  st.markdown("発走20:35以降のミッドナイトレースを対象に、PRO予想の1〜3列目データを用いた最適な組み合わせパターンや回収率を自動探索します。")

  df_parsed = get_parsed_df()

  if not df_parsed.empty:
    midnight_df = df_parsed[df_parsed["発走時間"].apply(lambda t: is_midnight_time(str(t)))].copy()

    st.markdown(f"🌙 抽出されたミッドナイト予想データ数: **{len(midnight_df):,} 件**")

    if st.button("🚀 ミッドナイトAI自動探索を実行する", type="primary"):
      with st.spinner("AIが最適な買い目パターンと回収率を解析中..."):
        exploration_results = {}

        for _, row in midnight_df.iterrows():
          author = row["予想家"]
          if author not in exploration_results:
            exploration_results[author] = {"総レース": 0, "総投資": 0, "総払戻": 0, "的中数": 0}

          exploration_results[author]["総レース"] += 1
          exploration_results[author]["総投資"] += row["投資額"]
          exploration_results[author]["総払戻"] += row["払戻合計"]
          if row["払戻合計"] > 0:
            exploration_results[author]["的中数"] += 1

        exp_list = []
        for auth, data in exploration_results.items():
          inv = data["総投資"]
          ret = data["総払戻"]
          roi = (ret / inv * 100) if inv > 0 else 0
          hit_rate = (data["的中数"] / data["総レース"] * 100) if data["総レース"] > 0 else 0

          exp_list.append({
              "予想家 / 戦略": auth,
              "対象レース数": data["総レース"],
              "的中数": data["的中数"],
              "的中率": f"{hit_rate:.1f}%",
              "総投資額": f"{inv:,} 円",
              "総払戻額": f"{ret:,} 円",
              "回収率 (ROI)": round(roi, 2),
          })

        if exp_list:
          df_exp_res = pd.DataFrame(exp_list).sort_values(by="回収率 (ROI)", ascending=False)
          st.success("✨ ミッドナイトAI自動探索が完了しました！")
          
          st.subheader("📊 ミッドナイト予想家別 回収率ランキング")
          show_df_responsive(df_exp_res, "ai_rank")
        else:
          st.warning("探索対象となるデータが見つかりませんでした。")
  else:
    st.warning("有効なデータが見つかりませんでした。")

# ==========================================
# 5. PRO予想 1×2列目MIX分析 (完全リニューアル版)
# ==========================================
elif app_mode == "🚧工事中｜PRO予想 1×2列目MIX分析":
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
          show_df_responsive(df_res, "mix_rank")

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
            show_df_responsive(
                df_author_races[[
                    "レースID",
                    "発走時間",
                    "結果着順",
                    "買い目原文",
                    "投資額",
                    "払戻合計",
                    "回収率",
                ]],
                "mix_detail",
            )
          else:
            st.info(
                f"{selected_detail_author} の的中データが現在ありません。"
            )
        else:
          st.warning("集計可能なデータが見つかりませんでした。")
  else:
    st.warning("有効なデータが見つかりませんでした。")

# ==========================================
# 6. 期待値（EV）買い目算出
# ==========================================
# 6. 買い目別 EVランキング
# ==========================================
elif app_mode == "🚧工事中｜🎯 買い目別 EVランキング":
  st.title("🎯 買い目別 EVランキング")
  st.markdown(
      "過去の全PRO予想から **「その買い目そのもの」の実績**（組み合わせ別・予想家×組み合わせ・"
      "会場別・軸車番別）をブレンドし、買い目1点ごとの推定的中率と推定回収率を出して、高い順に並べます。"
      "「どの予想家の・どの買い目に過去データの裏付けがあるか」を見るためのページです。"
  )
  with st.expander("📖 使い方・数字の見方（最初に読んでください）", expanded=True):
    st.markdown(
        "1. **対象日**と**対象レース**（ミッドナイトのみ／PRO全体）、**対象券種**を選ぶと、その日以降で結果未確定のレースの買い目が推定回収率の高い順に並びます。\n"
        "2. **推定回収率**は、その買い目を過去に買い続けた場合の回収率の推定です（100%超が一応の「割に合う」ライン）。**根拠内訳**に、どの実績を何点分ブレンドしたかが出ます。根拠サンプルが少ない買い目は数字が不安定なので、**最低サンプル点数**で足切りできます。\n"
        "3. **組み合わせ絞り込み**に「3-2」「3=2」のように入れると、その買い目だけに絞れます（パターンの検証用）。\n"
        "4. 現在オッズを下の欄に貼ると、その買い目は **推定的中率 × 現在オッズ** で計算し直され、順位も変わります。\n"
        "5. **予算配分案**は、最低期待回収率以上の買い目だけを予算内で上位から拾ったものです。推定は過去実績ベースで、的中を保証するものではありません。"
    )
  if ev_engine is None:
    st.error("ev_engine モジュールが読み込めませんでした。デプロイに ev_engine.py が含まれているか確認してください。")
  else:
    _, df_memos_ev, df_results_ev = load_replica_data()
    if df_memos_ev.empty or df_results_ev.empty:
      st.warning("有効なデータが見つかりませんでした。")
    else:
      c1, c2, c3 = st.columns(3)
      scope = c1.radio("対象レース", ["ミッドナイトのみ", "PRO全体"])
      target_date = c2.date_input("対象日（この日以降）", value=datetime.now().date())
      budget = c3.number_input("予算（円）", min_value=100, max_value=100000, value=1500, step=100)
      c4, c5, c6 = st.columns(3)
      min_ev_pct = c4.slider("最低期待回収率（%）", 50, 300, 80)
      bet_types = c5.multiselect(
          "対象券種", ev_engine.BET_TYPES, default=["2車単", "ワイド"]
      )
      min_sample = c6.number_input(
          "最低サンプル点数（根拠）", min_value=0, max_value=100000, value=0, step=50
      )
      combo_text = st.text_input(
          "組み合わせ絞り込み（任意）: 例 3-2 / 3=2",
          placeholder="3-2",
      )
      odds_text = st.text_area(
          "現在オッズ（任意）: 1行ごとに「レースID,券種,買い目,オッズ」",
          placeholder="20261102_名古屋_7,2車単,3-2,15.6",
      )
      odds_map = {}
      for line in odds_text.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 4:
          try:
            odds_map[(parts[0], parts[1], parts[2])] = float(parts[3])
          except ValueError:
            pass

      memos_ev, results_ev = ev_engine.frames_to_inputs(df_memos_ev, df_results_ev)
      stats_ev = ev_engine.build_history(memos_ev, results_ev)
      ranked_ev = ev_engine.score_upcoming(
          memos_ev,
          results_ev,
          stats_ev,
          midnight_only=(scope == "ミッドナイトのみ"),
          bet_types=bet_types or None,
          odds_map=odds_map,
          from_date=target_date.strftime("%Y%m%d"),
      )

      # 絞り込み（サンプル点数・組み合わせ）
      if not ranked_ev.empty and min_sample > 0:
        ranked_ev = ranked_ev[ranked_ev["根拠サンプル点数"] >= min_sample]

      def _combo_key(bet_type, combo):
        parts = re.split(r"[-=]", str(combo))
        if bet_type in ("ワイド", "2車複", "3連複"):
          parts = sorted(parts, key=lambda x: int(x) if x.isdigit() else 0)
        return "-".join(parts)

      combo_q = (
          combo_text.replace("→", "-").replace("＞", "-").strip()
      )
      if not ranked_ev.empty and combo_q:
        q_parts = re.split(r"[-=]", combo_q)
        q_keys = {
            "-".join(q_parts),
            "-".join(
                sorted(q_parts, key=lambda x: int(x) if x.isdigit() else 0)
            ),
        }
        ranked_ev = ranked_ev[
            ranked_ev.apply(
                lambda r: _combo_key(r["券種"], r["買い目"]) in q_keys, axis=1
            )
        ]

      if ranked_ev.empty:
        diag = ev_engine.diagnose(memos_ev, results_ev, from_date=target_date.strftime("%Y%m%d"))
        st.warning("この条件では候補が0件でした。データ状況は次のとおりです。")
        st.markdown(
            f"- DB内の予想つきレース: {diag['races_with_pred']}件（うち結果未登録: {diag['unresulted_races']}件）\n"
            f"- 対象日（{target_date}）以降で結果未登録のレース: {diag['unresulted_from_date']}件\n"
            f"- 予想メモの最新日: {diag['latest_date']}"
        )
        st.info(
            "結果が未登録のレースが無いと候補は出ません。予想メモがDBに同期されていない開催"
            "（例：当日のミッドナイト）も対象外になります。表示の確認だけなら、対象日を過去に変えると"
            "結果未登録のまま残っているレースでランキングが見られます。"
            "サンプル点数や組み合わせで絞り込んでいる場合は、その条件を緩めると出ることがあります。"
        )
      else:
        alloc_ev, summary_ev = ev_engine.allocate(
            ranked_ev, budget_yen=int(budget), min_ev=min_ev_pct / 100.0
        )
        st.subheader(
            f"💴 予算配分案（{summary_ev['points']}点 / {summary_ev['amount']:,}円）"
        )
        if alloc_ev.empty:
          st.info("最低期待回収率を超える買い目がありませんでした。下のランキングから個別に選んでください。")
        else:
          show_df_responsive(alloc_ev, "ev_alloc")
        st.subheader("📊 EVランキング（全候補）")
        show_df_responsive(ranked_ev, "ev_rank")
      st.caption("推定回収率は「その買い目そのもの」の過去実績（組み合わせ・予想家・会場・軸車番）のブレンド推定であり、的中や回収を保証するものではありません。")


elif app_mode == "🎯 EV v2 確率モデル買い目算出":
  st.title("🎯 EV v2 確率モデル買い目算出")
  st.markdown(
      "player_stats（競走得点・勝率・AI印）と当日のPRO予想から **各車の着順確率** を作り、"
      "Harville式（着順別確率版）で買い目ごとの的中確率に展開して、期待値の高い順に並べます。"
      "v1（過去の買い目パターンの払戻実績ベース）とは推定の作り方が異なるので、"
      "walk-forwardバックテストでの実測回収率とあわせて判断してください。"
  )
  if ev_v2 is None:
    st.error("ev_v2 モジュールが読み込めませんでした。デプロイに ev_v2.py が含まれているか確認してください。")
  else:
    df_players_v2, df_memos_v2, df_results_v2 = load_replica_data()
    if df_memos_v2.empty or df_results_v2.empty:
      st.warning("有効なデータが見つかりませんでした。")
    else:
      c1, c2, c3 = st.columns(3)
      scope_v2 = c1.radio("対象レース", ["ミッドナイトのみ", "PRO全体"], key="v2_scope")
      target_date_v2 = c2.date_input("対象日（この日以降）", value=datetime.now().date(), key="v2_date")
      budget_v2 = c3.number_input("予算（円）", min_value=100, max_value=100000, value=1500, step=100, key="v2_budget")
      c4, c5, c6 = st.columns(3)
      min_ev_v2 = c4.slider("最低期待回収率（%）", 50, 300, 100, key="v2_min_ev")
      bet_types_v2 = c5.multiselect("対象券種", ev_v2.BET_TYPES, default=["2車単", "ワイド"], key="v2_bet_types")
      require_pro_v2 = c6.checkbox("PRO予想があるレースのみ", value=True, key="v2_require_pro")
      odds_text_v2 = st.text_area(
          "現在オッズ（任意）: 1行ごとに「レースID,券種,買い目,オッズ」",
          placeholder="20261102_名古屋_7R_d1,2車単,3-2,15.6",
          key="v2_odds",
      )
      odds_map_v2 = {}
      for line in odds_text_v2.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 4:
          try:
            odds_map_v2[(parts[0], parts[1], parts[2])] = float(parts[3])
          except ValueError:
            pass

      memos_v2, results_v2 = ev_engine.frames_to_inputs(df_memos_v2, df_results_v2)
      stats_v2 = ev_v2.build_history_v2(memos_v2, results_v2)
      players_by_race_v2 = ev_v2.frames_to_players(df_players_v2)
      ranked_v2 = ev_v2.score_upcoming_v2(
          memos_v2,
          results_v2,
          players_by_race_v2,
          stats_v2,
          midnight_only=(scope_v2 == "ミッドナイトのみ"),
          bet_types=bet_types_v2 or None,
          odds_map=odds_map_v2,
          from_date=target_date_v2.strftime("%Y%m%d"),
          require_pro=require_pro_v2,
      )
      if ranked_v2.empty:
        st.warning("この条件では候補が0件でした。対象日を過去に変えるか、PRO予想のあるレースのみのチェックを外してみてください。")
      else:
        alloc_v2, summary_v2 = ev_engine.allocate(
            ranked_v2, budget_yen=int(budget_v2), min_ev=min_ev_v2 / 100.0
        )
        st.subheader(f"💴 予算配分案（{summary_v2['points']}点 / {summary_v2['amount']:,}円）")
        if alloc_v2.empty:
          st.info("最低期待回収率を超える買い目がありませんでした。下のランキングから個別に選んでください。")
        else:
          show_df_responsive(alloc_v2, "ev2_alloc")
        st.subheader("📊 EVランキング v2（全候補）")
        show_df_responsive(ranked_v2, "ev2_rank")
      st.caption("推定回収率は確率モデル（選手データ＋PRO支持＋車番の着順傾向）と、オッズ未入力の場合は券種ごとの的中時平均払戻から推定したもので、的中や回収を保証するものではありません。")


elif app_mode == "🧪 EVバックテスト（実力測定）":
  st.title("🧪 EVバックテスト（実力測定）")
  st.markdown(
      "過去のレースで「その日より前のデータだけで学習 → 当日の買い目を算出 → 実際の払戻と照合」を繰り返し、"
      "**もしこの買い方をしていたら回収率は何%だったか** を測る健康診断ページです。"
      "未来の情報は一切使わない（walk-forward）ので、出てきた数字がそのまま実力の目安になります。"
  )
  if backtest_ev is None or ev_engine is None:
    st.error("backtest_ev / ev_engine モジュールが読み込めませんでした。デプロイに backtest_ev.py と ev_engine.py が含まれているか確認してください。")
  else:
    df_players_bt, df_memos_bt, df_results_bt = load_replica_data()
    if df_memos_bt.empty or df_results_bt.empty:
      st.warning("有効なデータが見つかりませんでした。")
    else:
      c1, c2, c3 = st.columns(3)
      engine_bt = c1.radio("測定するエンジン", ["v2（確率モデル）", "v1（過去実績ベース）"], key="bt_engine")
      scope_bt = c2.radio("対象レース", ["ミッドナイトのみ", "全体"], key="bt_scope")
      days_bt = c3.slider("検証する日数（直近）", 7, 60, 14, key="bt_days")
      c4, c5, c6 = st.columns(3)
      budget_bt = c4.number_input("1日の予算（円）", min_value=100, max_value=100000, value=1500, step=100, key="bt_budget")
      min_train_bt = c5.number_input("学習に必要な最低レース数", min_value=50, max_value=5000, value=200, step=50, key="bt_min_train")
      bet_types_bt = c6.multiselect("対象券種", ev_engine.BET_TYPES, default=["2車単", "ワイド"], key="bt_bet_types")
      st.caption("日数が多いほど信頼度は上がりますが、計算時間も長くなります（v2・14日で数分程度が目安）。")
      if st.button("▶ バックテストを実行", key="bt_run"):
        scorer_bt = None
        if engine_bt.startswith("v2"):
          if ev_v2 is None:
            st.error("ev_v2 モジュールが読み込めませんでした。デプロイに ev_v2.py が含まれているか確認してください。")
          else:
            players_bt = ev_v2.frames_to_players(df_players_bt)
            scorer_bt = backtest_ev.v2_scorer(
                players_bt,
                midnight_only=(scope_bt == "ミッドナイトのみ"),
                bet_types=bet_types_bt or None,
            )
        else:
          scorer_bt = backtest_ev.v1_scorer(
              midnight_only=(scope_bt == "ミッドナイトのみ"),
              bet_types=bet_types_bt or None,
          )
        if scorer_bt is not None:
          memos_bt, results_by_id_bt = ev_engine.frames_to_inputs(df_memos_bt, df_results_bt)
          bar_bt = st.progress(0.0)
          status_bt = st.empty()
          state_bt = {"done": 0}

          def _bt_progress(date, n_train, n_cand):
            state_bt["done"] += 1
            bar_bt.progress(min(state_bt["done"] / max(int(days_bt), 1), 1.0))
            status_bt.write(f"検証中: {date}（学習 {n_train} レース / 当日候補 {n_cand} 点）")

          with st.spinner("バックテスト計算中…"):
            summary_bt, picks_bt = backtest_ev.walk_forward(
                memos_bt,
                results_by_id_bt,
                scorer_bt,
                budget_per_day=int(budget_bt),
                min_train_races=int(min_train_bt),
                recent_days=int(days_bt),
                progress=_bt_progress,
            )
          st.session_state["bt_summary"] = summary_bt
          st.session_state["bt_picks"] = picks_bt
      if "bt_summary" in st.session_state:
        summary_show = st.session_state["bt_summary"]
        picks_show = st.session_state["bt_picks"]
        st.subheader("📊 結果サマリー（戦略別）")
        show_df_responsive(summary_show, "bt_summary")
        row10 = summary_show[summary_show["戦略"] == "EV>=1.0"]
        if not row10.empty:
          r10 = row10.iloc[0]
          st.metric("EV>=1.0 戦略の回収率", f"{r10['回収率%']}%")
          st.write(
              f"対象 {int(r10['対象日数'])} 日 ／ {int(r10['点数'])} 点 ／ "
              f"投資 {int(r10['総投資']):,}円 → 払戻 {int(r10['総払戻']):,}円 ／ 的中 {int(r10['的中点数'])} 点"
          )
        if picks_show is not None and not picks_show.empty:
          st.subheader("🎯 採用ピック明細")
          show_df_responsive(picks_show, "bt_picks")
          st.download_button(
              "ピック明細をCSVでダウンロード",
              picks_show.to_csv(index=False).encode("utf-8-sig"),
              file_name="backtest_picks.csv",
              mime="text/csv",
              key="bt_csv",
          )
      st.caption("回収率は過去データでの検証結果であり、将来の的中や回収を保証するものではありません。点数が少ないうちは数字が大きく振れるので、日数を増やしてから判断してください。")


elif app_mode == "📊 PRO傾向＆会場別 勝ち筋分析":
  st.title("📊 PRO傾向＆会場別 勝ち筋分析")
  st.markdown(
      "保存されている全PRO予想（結果が確定したレースのみ）から、**PROごとの会場の得意・不得意**と"
      "**買い方の傾向**、**PRO同士のMIXパターンの実績**を自動集計します。"
      "PRO予想のデータが増えれば、数字は自動で更新されます。"
  )
  if pro_trends is None:
    st.error("pro_trends モジュールが読み込めませんでした。デプロイに pro_trends.py が含まれているか確認してください。")
  else:
    df_pt = get_parsed_df()
    if df_pt.empty:
      st.warning("有効なデータが見つかりませんでした。")
    else:
      df_settled_pt = pro_trends.settled_only(df_pt)
      st.caption(f"集計対象: 結果確定 {len(df_settled_pt):,} 件のPRO予想（保存済み全 {len(df_pt):,} 件中）")
      min_races_pt = st.slider(
          "会場別集計の最低レース数（これ未満の組み合わせは一覧に出しません）",
          5, 60, 20, step=5,
      )

      st.subheader("🏆 勝ち筋ランキング（PRO × 会場）")
      vs_pt = pro_trends.venue_stats(df_pt, min_races=min_races_pt)
      if vs_pt.empty:
        st.info("条件に合う組み合わせがありません。最低レース数を下げてみてください。")
      else:
        top_pt = vs_pt.head(10)
        bullets_pt = "\n".join(
            f"- **{r['予想家']} × {r['会場']}**: 回収率 {r['回収率%']}%"
            f"（{int(r['レース数'])}レース / 的中率 {r['的中レース率%']}% / 信頼度 {r['信頼度']}）"
            for _, r in top_pt.iterrows()
        )
        st.markdown("**回収率が高い組み合わせ 上位10**\n\n" + bullets_pt)
        show_df_responsive(vs_pt, "pt_venue_rank")

      st.subheader("🏟️ 会場マトリクス（回収率%）")
      piv_pt = pro_trends.venue_pivot(df_pt, min_races=min_races_pt)
      if piv_pt.empty:
        st.info("表示できるマトリクスがありません。")
      else:
        show_df_responsive(piv_pt.reset_index(), "pt_venue_pivot")
        st.caption("空欄は最低レース数に満たない（データ不足）の組み合わせです。100%超は「その会場では払戻が投資を上回った」組み合わせです。")

      st.subheader("🎯 PRO別 買い方スタイル")
      ss_pt = pro_trends.style_stats(df_pt)
      show_df_responsive(ss_pt, "pt_style")
      st.caption("軸の1着包含率 = 軸（1列目）に実際の1着が含まれていた割合。着順カバー率 = フォーメーションが実際の1〜3着をすべて含んでいた割合。")

      st.subheader("🏷️ PROタイプ分類（一発型か安定型か）")
      if not hasattr(pro_trends, "type_stats"):
        st.info("更新の反映待ちです。少し待ってからページを再読み込みしてください。")
      else:
        ts_pt = pro_trends.type_stats(df_pt)
        show_df_responsive(ts_pt, "pt_type")
        st.caption(
            "型の自動判定: 大当たり寄与（単レースで投資の3倍以上返った払戻が総払戻に占める割合）が55%以上なら「一発型」、"
            "的中率50%以上かつ的中時の平均払戻が1,600円以下なら「安定・拡散型」、それ以外は「バランス型」。"
            "一発型は丸乗りすると外れが続くため、下のサテライト・シミュレーターのように条件付きで小さく混ぜる使い方が向いています。"
        )

      st.subheader("🤝 MIXパターン検証（PRO 2人の相性）")
      authors_pt = sorted(df_pt["予想家"].unique().tolist())
      c_pt1, c_pt2 = st.columns(2)
      a_pt = c_pt1.selectbox("PRO A（主軸にしたい人）", authors_pt, index=0, key="pt_a")
      b_options_pt = [x for x in authors_pt if x != a_pt]
      b_pt = c_pt2.selectbox("PRO B（組み合わせる人）", b_options_pt, index=0, key="pt_b")
      if st.button("🔍 MIXパターンを検証する", type="primary"):
        ps_pt = pro_trends.pair_summary(df_pt, a_pt, b_pt)
        if ps_pt.empty:
          st.info("2人に共通するレースがありませんでした。")
        else:
          show_df_responsive(ps_pt, "pt_pair")
          _, _, df_results_pt = load_replica_data()
          res_map_pt = {}
          if not df_results_pt.empty and "レースID" in df_results_pt.columns:
            for _, rr in df_results_pt.iterrows():
              res_map_pt[str(rr["レースID"])] = (
                  rr.get("1着車番"), rr.get("2着車番"), rr.get("3着車番"), rr.get("払戻金JSON"),
              )
          m_pt = pro_trends.pair_frames(df_pt, a_pt, b_pt)
          agg_pt = {}
          for _, r in m_pt.iterrows():
            info_pt = res_map_pt.get(str(r["レースID"]))
            if info_pt is None:
              continue
            r1x, r2x, r3x, pay_json_pt = info_pt
            if pd.isnull(r1x) or pd.isnull(r2x) or pd.isnull(r3x):
              continue
            for pat_name, pat_text in pro_trends.mix_formations(r["cols_A"], r["cols_B"]).items():
              if pat_text is None:
                continue
              try:
                _, pts_x, cost_x, got_x, *_rest = parse_bet_string_detailed_rows(
                    pat_text, r1x, r2x, r3x, 0, 0, 0, pay_json_pt
                )
              except Exception:
                continue
              d_pt = agg_pt.setdefault(pat_name, {"races": 0, "pts": 0, "cost": 0, "got": 0})
              d_pt["races"] += 1
              d_pt["pts"] += pts_x
              d_pt["cost"] += cost_x
              d_pt["got"] += got_x
          mix_rows_pt = []
          for pat_name, d_pt in agg_pt.items():
            if d_pt["races"] == 0:
              continue
            disp_pt = (
                pat_name.replace("A軸", f"{a_pt}軸").replace("B軸", f"{b_pt}軸")
                .replace("A相手", f"{a_pt}相手").replace("B相手", f"{b_pt}相手")
            )
            mix_rows_pt.append({
                "MIXパターン": disp_pt,
                "対象レース数": d_pt["races"],
                "平均点数": round(d_pt["pts"] / d_pt["races"], 1),
                "投資額": f"{d_pt['cost']:,}円",
                "払戻額": f"{d_pt['got']:,}円",
                "回収率%": round(d_pt["got"] / d_pt["cost"] * 100, 1) if d_pt["cost"] else 0,
            })
          if mix_rows_pt:
            st.markdown("**合成MIXフォーメーションの実績（実際の払戻金で計算）**")
            show_df_responsive(
                pd.DataFrame(mix_rows_pt).sort_values("回収率%", ascending=False), "pt_mix"
            )
            st.caption("「軸一致ミックス」は2人の軸（1列目）が一致したレースだけが対象です。単独成績とは対象レースが異なるため、回収率の比較は目安として見てください。")
      st.subheader("🛰️ サテライト・シミュレーター（主軸＋条件付きで混ぜる）")
      if not hasattr(pro_trends, "satellite_simulation"):
        st.info("更新の反映待ちです。少し待ってからページを再読み込みしてください。")
      st.markdown(
          "主軸PROを中心に、サテライトPRO（一発型など）を**条件が合ったレースだけ・予算の一部だけ**混ぜたら"
          "回収率がどう変わったかを、過去データで試算します。"
      )
      c_pt3, c_pt4 = st.columns(2)
      base_options_pt = ["ふるだび"] if "ふるだび" in authors_pt else authors_pt[:1]
      base_pt = c_pt3.selectbox("主軸PRO", authors_pt, index=authors_pt.index(base_options_pt[0]), key="pt_base")
      sat_options_pt = [x for x in authors_pt if x != base_pt]
      sat_default_pt = "てんぐり" if "てんぐり" in sat_options_pt else sat_options_pt[0]
      sat_pt = c_pt4.selectbox("サテライトPRO", sat_options_pt, index=sat_options_pt.index(sat_default_pt), key="pt_sat")
      sat_conditions_pt = getattr(
          pro_trends, "SAT_CONDITIONS",
          ["常時", "得意会場のみ", "軸一致時のみ", "軸不一致時のみ"],
      )
      cond_pt = st.selectbox(
          "サテライトを作動させる条件",
          sat_conditions_pt,
          index=sat_conditions_pt.index("得意会場のみ"),
          key="pt_cond",
          help="「得意会場のみ」は、サテライトPROの会場別回収率が100%以上（15レース以上）の会場でのみ作動します。",
      )
      share_pt = st.slider("サテライトへの配分（作動したレースの予算のうち）", 5, 40, 20, step=5, key="pt_share")
      if st.button("🛰️ サテライト試算を実行する", type="primary") and hasattr(pro_trends, "satellite_simulation"):
        rows_sat = []
        for sh in [0.1, 0.2, 0.3]:
          sim_pt = pro_trends.satellite_simulation(
              df_pt, base_pt, sat_pt, share=sh, condition=cond_pt
          )
          if sim_pt:
            rows_sat.append({
                "サテライト配分": f"{int(sh * 100)}%",
                "共通レース数": sim_pt["共通レース数"],
                "作動レース数": sim_pt["作動レース数"],
                f"{base_pt}単独 回収率%": sim_pt["主軸単独 回収率%"],
                f"{sat_pt}単独 回収率%": sim_pt["サテライト単独 回収率%"],
                "合成 回収率%": sim_pt["合成 回収率%"],
            })
        if not rows_sat:
          st.info("2人に共通するレースがありませんでした。")
        else:
          cur_pt = pro_trends.satellite_simulation(
              df_pt, base_pt, sat_pt, share=share_pt / 100, condition=cond_pt
          )
          m_pt1, m_pt2, m_pt3 = st.columns(3)
          m_pt1.metric(f"{base_pt}単独（共通レース）", f"{cur_pt['主軸単独 回収率%']}%")
          m_pt2.metric(f"合成（配分{share_pt}%）", f"{cur_pt['合成 回収率%']}%",
                       delta=f"{cur_pt['合成 回収率%'] - cur_pt['主軸単独 回収率%']:+.1f}% vs 主軸単独")
          m_pt3.metric("作動レース数", f"{cur_pt['作動レース数']} / {cur_pt['共通レース数']}")
          show_df_responsive(pd.DataFrame(rows_sat), "pt_sat_table")
          st.caption("試算方法: 1レースの予算を1,000円とし、作動時は主軸とサテライトに予算を配分（払戻は配分比率で比例計算）、非作動時は主軸に全額。共通レース（結果確定・両者とも予想あり）のみが対象です。")
      st.caption("すべて過去データの集計で、将来の的中や回収を保証するものではありません。レース数が少ない組み合わせは数字が大きく振れます。")


elif app_mode == "🧮 ミッドナイト買い目 回収率チェッカー":
  import datetime as _dt

  import midnight_checker

  st.title("🧮 ミッドナイト買い目 回収率チェッカー")
  st.caption("指定した日のミッドナイト（発走20:35以降）の全レースに、その買い目を1点ずつ買った場合の回収率と、根拠となったレース一覧を表示します。")

  _jst = _dt.timezone(_dt.timedelta(hours=9))
  c1, c2, c3 = st.columns(3)
  target_date = c1.date_input("対象日", value=_dt.datetime.now(_jst).date())
  bet_type = c2.selectbox("券種", ["ワイド", "2車単", "2車複", "3連複", "3連単"])
  stake = c3.number_input("1点の金額（円）", min_value=100, step=100, value=100)

  _n_cars = 2 if bet_type in ("ワイド", "2車単", "2車複") else 3
  _ordered = bet_type in ("2車単", "3連単")
  _labels = ["1着", "2着", "3着"] if _ordered else ["車番1", "車番2", "車番3"]
  _defaults = [3, 2, 1]
  _car_cols = st.columns(_n_cars)
  cars = []
  for i in range(_n_cars):
    cars.append(
        _car_cols[i].selectbox(
            _labels[i],
            list(range(1, 10)),
            index=_defaults[i] - 1,
            key=f"mc_car_{bet_type}_{i}",
        )
    )

  if len(set(cars)) != len(cars):
    st.error("車番が重複しています。すべて異なる車番を選んでください。")
  else:
    _, _, _df_res = load_replica_data()
    if _df_res.empty:
      st.warning("結果データを読み込めませんでした。")
    else:
      _summary, _rows = midnight_checker.check(
          _df_res.to_dict("records"), target_date, bet_type, cars, stake=int(stake)
      )
      st.subheader(f"🌙 {bet_type} {_summary['組番']} × {target_date} ミッドナイト")
      m1, m2, m3, m4 = st.columns(4)
      m1.metric("回収率", f"{_summary['回収率']:.1f}%")
      m2.metric("収支", f"{_summary['収支']:+,}円")
      m3.metric("投資", f"{_summary['総投資']:,}円")
      m4.metric("払戻", f"{_summary['総払戻']:,}円")
      st.write(
          f"対象 {_summary['対象レース数']} レース ／ 的中 {_summary['的中レース数']} レース"
          f"（的中率 {_summary['的中率']:.1f}%）"
      )
      if _rows:
        _df_show = pd.DataFrame(_rows)
        _df_show["判定"] = _df_show["的中"].map({True: "◎ 的中", False: "―"})
        _df_show = _df_show.drop(columns=["的中"])[
            ["会場", "レース", "発走", "結果", "払戻金", "判定"]
        ]
        show_df_responsive(_df_show, "checker_rows", head_cols=2)
      else:
        st.info("この日のミッドナイトの結果データはまだありません（レース前、または未同期の可能性があります）。")
    st.caption("払戻金は公式払戻（100円あたり）をもとに、1点の金額へ比例換算しています。")

# ==========================================
# 11. ミッドナイト特選 123BOX決着リスト
# ==========================================
elif app_mode == "🎁 ミッドナイト特選 123BOX決着リスト":
  import midnight_checker as _mc_box

  st.title("🎁 ミッドナイト特選 123BOX決着リスト")
  st.caption(
      "ミッドナイト（発走20:35以降）の特選で、1・2・3番車がそのまま1〜3着を占めた（順序不問＝BOX決着）レースを全期間から抽出します。"
      "DBに特選フラグが無いため、特選は「その会場・その開催日の最終レース」で代用しています。"
  )

  tokusen_only_box = st.checkbox("特選（その日の最終レース）のみ", value=True)

  _, _, _df_res_box = load_replica_data()
  if _df_res_box.empty:
    st.warning("結果データを読み込めませんでした。")
  else:
    _summary_box, _rows_box = _mc_box.box123_scan(
        _df_res_box.to_dict("records"), tokusen_only=tokusen_only_box
    )
    _n_target = _summary_box["対象レース数"]
    _n_hit = _summary_box["該当レース数"]
    if _rows_box:
      _df_box = pd.DataFrame(_rows_box)
      _ret_3f = int(_df_box["3連複払戻"].sum())
      _ret_3t = int(_df_box["3連単払戻"].sum())
      _rate_hit = _n_hit / _n_target * 100 if _n_target else 0.0
      _rate_3f = _ret_3f / (_n_target * 100) * 100 if _n_target else 0.0
      _rate_3t = _ret_3t / (_n_target * 600) * 100 if _n_target else 0.0

      m1, m2, m3, m4 = st.columns(4)
      m1.metric("該当レース", f"{_n_hit} 件")
      m2.metric("BOX決着の割合", f"{_rate_hit:.1f}%", help=f"対象 {_n_target} レース中")
      m3.metric("3連複1点買い 回収率", f"{_rate_3f:.1f}%")
      m4.metric("3連単BOX6点 回収率", f"{_rate_3t:.1f}%")
      st.write(
          f"対象 {_n_target} レース ／ 着順がちょうど 3-2-1 だったのは {_summary_box['着順3-2-1の件数']} 件。"
          f"毎回買っていた場合: 3連複（100円×全レース）払戻合計 {_ret_3f:,}円、"
          f"3連単BOX（600円×全レース）払戻合計 {_ret_3t:,}円。"
      )
      show_df_responsive(_df_box, "box123_rows", head_cols=2)
      st.download_button(
          "📥 一覧をCSVでダウンロード",
          _df_box.to_csv(index=False).encode("utf-8-sig"),
          file_name="midnight_tokusen_123box.csv",
          mime="text/csv",
      )
    else:
      st.info(f"該当レースはありませんでした（対象 {_n_target} レース）。")
