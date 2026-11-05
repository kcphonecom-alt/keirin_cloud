# Mac スケジュール全自動パック

## なぜ必要か
GitHub Actions（海外データセンターのIP）から WINTICKET の PRO予想ページを開くと、予想欄が「予想はまだありません」と空で返る（出走表・結果は取れる）。ブラウザ偽装（UA・言語・タイムゾーン）でも解けなかった（2026-11-04 判定）。日本の家庭用IP（自宅Mac）からは通常どおり取得できる。
→ 取得処理そのものを Mac に移し、launchd でスケジュール起動して全自動化する。

## 導入（1回だけ）
1. このフォルダの `install_mac_scheduler.sh` を、Mac の `pinpoint.py` と同じフォルダ（例: `~/Documents/keirin_v3`）に置く
2. ターミナルでそのフォルダへ移動し `bash install_mac_scheduler.sh`
3. `env.sh` が無い場合は雛形が作られるので、`DATABASE_URL=` の後ろに接続URL（手動実行で使っているもの）を書いて、もう一度 2 を実行
   ※ シェルに DATABASE_URL が設定済みの環境では自動で取り込まれる

## スケジュール（日本時間・毎日）
| 時刻 | 実行 | 内容 |
|---|---|---|
| 07:30 / 09:00 / 12:00 / 15:00 / 18:00 / 21:00 / 23:00 | pinpoint.py | PRO予想のみ（当日分。当日中に消えるため複数回） |
| 23:50 | daily_update.py | 出走表・結果・PRO予想（当日分フル） |
| 04:30 | daily_update.py（DAYS_OFFSET=1） | 前日分の取りこぼし回収 |
| 03:30 | backfill_midnight.py | ミッドナイト不足分の補完（対象が無くなれば空振り終了） |

## 運用
- ログ: プロジェクトフォルダの `logs/`（pinpoint.log / daily.log など）
- 停止: `bash uninstall_mac_scheduler.sh`（plist を削除。スクリプトとログは残る）
- 条件: Mac が起動してログインしていること。スリープしていた回は起動直後にまとめて実行される
- GitHub Actions の daily_update は、Mac 側の動作が数日安定してから schedule を止める（手動実行は残す）。出走表・結果だけなら Actions でも取れるため、Mac 停止時の保険になる
