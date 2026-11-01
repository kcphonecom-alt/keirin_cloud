# daily_update.py (Playwright対応 PRO予想・出走表・結果 完全統合・決定版 + 正しい開催日ID・1〜3列目完全分離対応)

import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
import psycopg2
from psycopg2.extras import execute_values
from playwright.async_api import async_playwright

import os
import sys

try:
    import psycopg2
except ImportError:
    import psycopg2_binary as psycopg2

# 環境変数からのみ取得する（公開リポジトリのため、接続文字列をコードに書かない）
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("[致命的エラー] 環境変数 DATABASE_URL が設定されていません。GitHub Secrets / ローカル環境変数を確認してください。")
    sys.exit(1)

STADIUM_MASTER = {
    "青森": {"en": "aomori", "code": "12"},
    "いわき平": {"en": "iwakidaira", "code": "13"},
    "弥彦": {"en": "yahiko", "code": "21"},
    "前橋": {"en": "maebashi", "code": "22"},
    "取手": {"en": "toride", "code": "23"},
    "宇都宮": {"en": "utsunomiya", "code": "24"},
    "大宮": {"en": "omiya", "code": "25"},
    "西武園": {"en": "seibuen", "code": "26"},
    "京王閣": {"en": "keiokaku", "code": "27"},
    "立川": {"en": "tachikawa", "code": "28"},
    "松戸": {"en": "matsudo", "code": "31"},
    "川崎": {"en": "kawasaki", "code": "34"},
    "平塚": {"en": "hiratsuka", "code": "35"},
    "小田原": {"en": "odawara", "code": "36"},
    "静岡": {"en": "shizuoka", "code": "38"},
    "名古屋": {"en": "nagoya", "code": "42"},
    "岐阜": {"en": "gifu", "code": "43"},
    "大垣": {"en": "ogaki", "code": "44"},
    "豊橋": {"en": "toyohashi", "code": "45"},
    "富山": {"en": "toyama", "code": "46"},
    "松阪": {"en": "matsusaka", "code": "47"},
    "四日市": {"en": "yokkaichi", "code": "48"},
    "奈良": {"en": "nara", "code": "53"},
    "和歌山": {"en": "wakayama", "code": "55"},
    "玉野": {"en": "tamano", "code": "61"},
    "広島": {"en": "hiroshima", "code": "62"},
    "防府": {"en": "hofu", "code": "63"},
    "小松島": {"en": "komatsushima", "code": "73"},
    "高知": {"en": "kochi", "code": "74"},
    "松山": {"en": "matsuyama", "code": "75"},
    "小倉": {"en": "kokura", "code": "81"},
    "久留米": {"en": "kurume", "code": "83"},
    "武雄": {"en": "takeo", "code": "84"},
    "佐世保": {"en": "sasebo", "code": "85"},
    "別府": {"en": "beppu", "code": "86"},
    "熊本": {"en": "kumamoto", "code": "87"},
}

PREFECTURES = {
    "北海道", "青森", "岩手", "宮城", "秋田", "山形", "福島", "茨城", "栃木", "群馬",
    "埼玉", "千葉", "東京", "神奈川", "新潟", "富山", "石川", "福井", "山梨", "長野",
    "岐阜", "静岡", "愛知", "三重", "滋賀", "京都", "大阪", "兵庫", "奈良", "和歌山",
    "鳥取", "島根", "岡山", "広島", "山口", "徳島", "香川", "愛媛", "高知", "福岡",
    "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島", "沖縄"
}

INVALID_NAMES = {
    "本命", "対抗", "単穴", "連下", "穴", "本線", "注目", "抑え", "推奨", "自信",
    "逃", "捲", "追", "両", "S", "H", "B"
}

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def describe_db_target():
    """ログ確認用に、接続先のホスト部分だけを返す（パスワードは出さない）。"""
    try:
        from urllib.parse import urlparse
        u = urlparse(DATABASE_URL)
        return f"{u.hostname}:{u.port}{u.path}"
    except Exception:
        return "(解析できませんでした)"

async def fetch_rendered_text(page, url):
    try:
        await page.goto(url, wait_until="networkidle", timeout=15000)
        try:
            await page.wait_for_selector("text=払戻金", timeout=4000)
        except Exception:
            await page.wait_for_timeout(2000)
            
        content = await page.evaluate("() => document.body.innerText")
        return content
    except Exception:
        pass
    return ""

def is_valid_race_page(text, venue_jp):
    if not text or len(text) < 500:
        return False
    if "ページが見つかりません" in text or "開催されていません" in text:
        return False
    if venue_jp not in text:
        return False
    return True

def extract_start_time(raw_text):
    if not raw_text:
        return None
    m = re.search(r"発走\s*([0-2]?[0-9]:[0-5][0-9])", raw_text)
    if m:
        return m.group(1)
    return None

def parse_bet_to_rows(raw_text):
    """
    買い目原文をハイフンやイコールで分割し、各列の車番を綺麗にパースする関数
    例: "3-124-124" -> 1列目:"3", 2列目:"1,2,4", 3列目:"1,2,4"
    """
    if not raw_text or not isinstance(raw_text, str):
        return "", "", ""

    normalized = raw_text.replace("ー", "-").replace("―", "-").replace("＝", "=").replace(" ", "").strip()
    parts = re.split(r"[-=]", normalized)

    def clean_block(part_str):
        if not part_str:
            return ""
        digits = re.findall(r'[1-9]', part_str)
        seen = set()
        unique_digits = []
        for d in digits:
            if d not in seen:
                seen.add(d)
                unique_digits.append(d)
        return ",".join(unique_digits)

    r1 = clean_block(parts[0]) if len(parts) >= 1 else ""
    r2 = clean_block(parts[1]) if len(parts) >= 2 else ""
    r3 = clean_block(parts[2]) if len(parts) >= 3 else ""

    if len(parts) == 2 and not r3:
        r3 = r2

    return r1, r2, r3

def parse_winticket_detailed_score(raw_text, stadium_hint, race_id, start_time_val):
    if not raw_text:
        return []
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]

    parsed_players = []
    score_indices = []
    for idx, line in enumerate(lines):
        if re.match(r"^[4-9][0-9]\.\d{2}$", line) or re.match(r"^1[0-1][0-9]\.\d{2}$", line):
            score_indices.append((idx, float(line)))

    for car_idx, (s_idx, score_val) in enumerate(score_indices):
        car_num = car_idx + 1
        if car_num > 9:
            break

        player_name = f"選手{car_num}"
        ai_eval = "-"
        run_style = "両"
        gear_val = "3.92"
        comment_str = "-"
        memo_str = ""
        pure_ints = []
        floats = []

        for offset in range(1, 14):
            if s_idx - offset >= 0:
                cand = lines[s_idx - offset]
                if cand in ["◎", "○", "▲", "△", "×", "注", "元", "✕"]:
                    ai_eval = cand
                elif not re.search(r"(車|枠|選手名|競走得点|勝率|対率|コメント|メモ|自力|自在|出走表|予想|期|歳|級班)", cand) \
                     and len(cand) >= 2 and len(cand) <= 6 \
                     and not cand.isdigit() \
                     and cand not in PREFECTURES \
                     and cand not in INVALID_NAMES \
                     and player_name.startswith("選手"):
                    clean_name = re.sub(r"^\d+", "", cand)
                    if len(clean_name) >= 2 and clean_name not in PREFECTURES and clean_name not in INVALID_NAMES:
                        player_name = clean_name

        next_s_idx = score_indices[car_idx + 1][0] if car_idx + 1 < len(score_indices) else len(lines)
        after_score_lines = lines[s_idx + 1 : next_s_idx]

        for b_line in after_score_lines:
            if b_line in ["逃", "捲", "追", "両"]:
                run_style = b_line
                continue
            if re.match(r"^[1-4]\.\d{2}$", b_line):
                gear_val = b_line
                continue
            if re.match(r"^\d+$", b_line):
                pure_ints.append(int(b_line))
            elif re.match(r"^\d+\.\d+$", b_line):
                floats.append(float(b_line))
            elif b_line == "メモあり":
                memo_str = "メモあり"
                continue
            elif len(b_line) > 2 and not re.match(r"^\d+[\.\-\/]?\d*$", b_line):
                if comment_str == "-":
                    comment_str = b_line

        stat_nums = pure_ints[:10] if len(pure_ints) >= 10 else pure_ints + [0] * (10 - len(pure_ints))

        parsed_players.append((
            race_id, stadium_hint, (car_num - 1) // 3 + 1, int(car_num), player_name,
            ai_eval, float(score_val), int(stat_nums[0]), int(stat_nums[1]), int(stat_nums[2]),
            run_style, int(stat_nums[3]), int(stat_nums[4]), int(stat_nums[5]),
            int(stat_nums[6]) if len(stat_nums) > 6 else 0, int(stat_nums[6]) if len(stat_nums) > 6 else 0,
            int(stat_nums[7]) if len(stat_nums) > 7 else 0, int(stat_nums[8]) if len(stat_nums) > 8 else 0,
            floats[0] if len(floats) > 0 else 0.0, floats[1] if len(floats) > 0 else 0.0, floats[2] if len(floats) > 2 else 0.0,
            gear_val, comment_str, memo_str, start_time_val,
        ))
    return parsed_players

def parse_pro_predictions_html(raw_text):
    if not raw_text or "PROユーザー予想" not in raw_text:
        return []
    
    pro_start = raw_text.find("PROユーザー予想")
    target_text = raw_text[pro_start:]
    
    for kw in ["買い目を選択してください", "投票シート", "WINTICKET", "投票ボックス"]:
        idx = target_text.find(kw)
        if idx != -1:
            target_text = target_text[:idx]

    lines = [l.strip() for l in target_text.split("\n") if l.strip()]

    ignore_set = {
        "PROユーザー予想", "プロフィール画像", "PRO", "リアクション", "レース情報", 
        "左矢印", "右矢印", "区切り", "3連単", "3連複", "2車単", "2車複", "2枠単", 
        "2枠複", "ワイド", "オッズ一覧", "投票受付中", "まもなく", "締切", "終了", "標準", "投票シート", "R", "的中"
    }

    predictions = []
    i = 0
    while i < len(lines):
        line = lines[i]
        
        if line == "PRO" and i > 0 and i + 1 < len(lines):
            author = lines[i - 1]
            time_ago = lines[i + 1]
            
            if author not in ignore_set and not author.isdigit():
                i += 2
                comment_lines = []
                bets = ""
                
                while i < len(lines):
                    next_line = lines[i]
                    if i + 1 < len(lines) and lines[i + 1] == "PRO":
                        break
                    
                    cleaned_bet = next_line.replace("ー", "-").replace("―", "-")
                    if re.match(r"^[0-9\-\=\/]+$", cleaned_bet) and len(cleaned_bet) <= 20 and ("-" in cleaned_bet or "=" in cleaned_bet):
                        bets = next_line
                    elif next_line not in ignore_set and not next_line.isdigit():
                        clean_comment = re.sub(r"\[.*?\]", "", next_line).strip()
                        if clean_comment and clean_comment not in ["現在、買い目:", "買い目:"]:
                            comment_lines.append(clean_comment)
                    i += 1

                if author and (comment_lines or bets):
                    full_comment = "\n".join(comment_lines)
                    predictions.append({
                        "予想家": author,
                        "投稿時間": time_ago,
                        "コメント": full_comment,
                        "買い目を抽出する": bets,
                        "買い目": bets
                    })
                continue
        i += 1
            
    return predictions

def parse_race_result_page(raw_text, race_id, start_time_val):
    if not raw_text or "結果はありません" in raw_text:
        return None

    lines = [l.strip() for l in raw_text.split('\n') if l.strip()]

    top3_cars = []
    parsing_rank = False
    for i, line in enumerate(lines):
        if line == "着" and i + 1 < len(lines) and (lines[i+1] in ["車", "車番"]):
            parsing_rank = True
            continue
        if parsing_rank:
            if line in ["1", "2", "3"] and i + 1 < len(lines):
                rank_num = int(line)
                car_num_str = lines[i+1]
                name_str = lines[i+2] if i + 2 < len(lines) else "-"
                if car_num_str.isdigit() and len(car_num_str) <= 2:
                    top3_cars.append({
                        "着順": rank_num, 
                        "車番": int(car_num_str), 
                        "選手名": name_str if not name_str.isdigit() else f"選手{car_num_str}"
                    })
            if len(top3_cars) >= 3:
                break
    top3_cars.sort(key=lambda x: x["着順"])

    payouts = []
    full_text = "\n".join(lines)
    ticket_types = ["2枠複", "2枠単", "2車複", "2車単", "3連複", "3連単", "ワイド", "単勝", "複勝"]
    
    for t_type in ticket_types:
        pos = full_text.find(t_type)
        if pos == -1:
            continue
        
        next_pos = len(full_text)
        for other in ticket_types:
            if other != t_type:
                p_other = full_text.find(other, pos + len(t_type))
                if p_other != -1 and p_other < next_pos:
                    next_pos = p_other
        
        for stop_word in ["競輪", "レース情報", "投票シート"]:
            p_stop = full_text.find(stop_word, pos + len(t_type))
            if p_stop != -1 and p_stop < next_pos:
                next_pos = p_stop

        section = full_text[pos:next_pos]
        if "未発売" in section:
            continue

        matches = re.findall(r'([0-9\=\-]+)\s+([0-9,]+円)\s+(\(\d+\))', section)
        for m in matches:
            comb = m[0].strip()
            price = m[1].strip()
            pop = m[2].strip()
            
            payouts.append({
                "賭け式": t_type,
                "組番": comb,
                "払戻金": price,
                "人気": pop
            })

    seen = set()
    unique_payouts = []
    for p in payouts:
        key = (p["賭け式"], p["組番"])
        if key not in seen:
            seen.add(key)
            unique_payouts.append(p)

    c1_car = top3_cars[0]["車番"] if len(top3_cars) > 0 else None
    c1_name = top3_cars[0]["選手名"] if len(top3_cars) > 0 else "-"
    c2_car = top3_cars[1]["車番"] if len(top3_cars) > 1 else None
    c2_name = top3_cars[1]["選手名"] if len(top3_cars) > 1 else "-"
    c3_car = top3_cars[2]["車番"] if len(top3_cars) > 2 else None
    c3_name = top3_cars[2]["選手名"] if len(top3_cars) > 2 else "-"

    if c1_car is not None and c2_car is not None and c3_car is not None:
        return (
            race_id,
            c1_car, c1_name,
            c2_car, c2_name,
            c3_car, c3_name,
            json.dumps(unique_payouts, ensure_ascii=False),
            start_time_val
        )
    return None

async def run_daily_update():
    print("=== 競輪日次自動アップデート処理（完全統合版）を開始します ===")
    # GitHub Actions等のUTC環境でも日本時間で日付判定する
    # DAYS_OFFSET: 手動実行で「昨日分」を対象にしたい場合に 1 を指定（結果の取りこぼし回収用）
    days_offset = int(os.environ.get("DAYS_OFFSET", "0") or "0")
    today_dt = datetime.now(timezone(timedelta(hours=9))) - timedelta(days=days_offset)
    print(f"対象日: {today_dt.strftime('%Y-%m-%d')} (JST, offset={days_offset}) / DB接続先: {describe_db_target()}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
    except Exception as e:
        # ここで正常終了するとGitHub Actionsが「成功」と誤表示するため、異常終了させる
        print(f"[致命的エラー] DB接続に失敗しました: {e}")
        raise SystemExit(1)

    updated_races_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for venue_jp, info in STADIUM_MASTER.items():
            v_en = info["en"]
            v_code = info["code"]

            matched_base = None
            matched_day = None

            # 🔄 過去4日間に遡って開催初日のコード（ベース日）を正確に探すロジック
            for days_ago in range(4):
                start_dt = today_dt - timedelta(days=days_ago)
                start_date_str = start_dt.strftime("%Y%m%d")
                test_base = f"{start_date_str}{v_code}"

                target_day_num = (today_dt.date() - start_dt.date()).days + 1
                if target_day_num < 1 or target_day_num > 7:
                    continue

                test_url = f"https://www.winticket.jp/keirin/{v_en}/racecard/{test_base}/{target_day_num}/1"
                txt = await fetch_rendered_text(page, test_url)
                
                if is_valid_race_page(txt, venue_jp):
                    matched_base = test_base
                    matched_day = str(target_day_num)
                    break

            if not matched_base:
                continue

            print(f"🏟️ 【{venue_jp}】（本日は {matched_day}日目 / 初日コード: {matched_base}）の開催を検知！")

            # 初日コードの最初8桁（例: "20261030"）を正しいベース日として抽出
            base_date_str = matched_base[:8]

            for r_num in range(1, 13):
                try:
                    card_url = f"https://www.winticket.jp/keirin/{v_en}/racecard/{matched_base}/{matched_day}/{r_num}"
                    pred_url = f"https://www.winticket.jp/keirin/{v_en}/predictions/{matched_base}/{matched_day}/{r_num}"
                    result_url = f"https://www.winticket.jp/keirin/{v_en}/raceresult/{matched_base}/{matched_day}/{r_num}"

                    # 正しい開催初日ベースの日付を使った一意のレースID
                    current_race_id = f"{base_date_str}_{venue_jp}_{r_num}R_d{matched_day}"
                    old_style_race_id = f"{base_date_str}{v_code}_{v_en}_{r_num}R_d{matched_day}"

                    card_text = await fetch_rendered_text(page, card_url)
                    if not is_valid_race_page(card_text, venue_jp):
                        break

                    start_time = extract_start_time(card_text)

                    # ※削除は「新しいデータが取れたテーブルだけ」差し替える。
                    #   取得失敗時に既存データを消してしまう事故を防ぐため、各INSERT直前に移動。

                    # 1. 出走表
                    players = parse_winticket_detailed_score(card_text, venue_jp, current_race_id, start_time)
                    if players:
                        cursor.execute('DELETE FROM player_stats WHERE "レースID" IN (%s, %s)', (current_race_id, old_style_race_id))
                        insert_player_query = """
                            INSERT INTO player_stats (
                                "レースID", "競輪場", "枠", "車", "選手名", "AI", "競走得点",
                                "S", "H", "B", "脚質", "1着", "2着", "3着", "3着外", "着外",
                                "差", "マ", "勝率", "2連対率", "3連対率", "ギヤ倍率", "コメント", "メモ", "発走時間"
                            ) VALUES %s
                        """
                        execute_values(cursor, insert_player_query, players)

                    # 2. PRO予想（買い目原文と各列を分離して保存）
                    pred_text = await fetch_rendered_text(page, pred_url)
                    parsed_preds = parse_pro_predictions_html(pred_text)
                    if parsed_preds:
                        cursor.execute('DELETE FROM race_memos WHERE "レースID" IN (%s, %s)', (current_race_id, old_style_race_id))
                        preview_lines = []
                        for pr in parsed_preds:
                            preview_lines.append(f"【{pr['予想家']} ({pr['投稿時間']})】\n{pr['コメント']}\n買い目: {pr['買い目']}\n")
                        preview_txt = "\n".join(preview_lines)

                        raw_bets = parsed_preds[0].get("買い目", "")
                        c1, c2, c3 = parse_bet_to_rows(raw_bets)

                        cursor.execute(
                            """
                                INSERT INTO race_memos (
                                    "レースID", "PRO予想テキスト", "構造化データJSON", "発走時間", 
                                    "買い目原文", "Pro予想買い目1", "Pro予想買い目2", "Pro予想買い目3"
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (current_race_id, preview_txt, json.dumps(parsed_preds, ensure_ascii=False), start_time, raw_bets, c1, c2, c3)
                        )

                    # 3. レース結果
                    result_text = await fetch_rendered_text(page, result_url)
                    res_data = parse_race_result_page(result_text, current_race_id, start_time)
                    if res_data:
                        cursor.execute('DELETE FROM race_results WHERE "レースID" IN (%s, %s)', (current_race_id, old_style_race_id))
                        cursor.execute(
                            """
                                INSERT INTO race_results (
                                    "レースID", "1着車番", "1着選手名", "2着車番", "2着選手名", "3着車番", "3着選手名", "払戻金JSON", "発走時間"
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            res_data
                        )

                    conn.commit()
                    updated_races_count += 1
                    res_flag = "有" if res_data else "未確定/無"
                    print(f"   └ [完了] {venue_jp} {r_num}R (出走:{len(players)}人, 予想:{len(parsed_preds)}件, 結果:{res_flag})")

                except Exception as race_ex:
                    conn.rollback()

        await browser.close()

    cursor.close()
    conn.close()
    print(f"=== 競輪日次自動アップデート処理が正常に完了しました (処理レース数: {updated_races_count}件) ===")

if __name__ == "__main__":
    asyncio.run(run_daily_update())