# Android で pinpoint を手動実行する（Termux + proot Ubuntu）

スケジュール登録なし・手動実行専用の構成。今使っている Android 端末で動かす。

## 仕組み
Termux 単体では Playwright のブラウザが動かない（Android 用ライブラリの都合）。そのため Termux の中に proot で Ubuntu を入れ、その中で Mac と同じ Python + Playwright を動かす。通信は端末の回線（自宅 Wi-Fi なら家庭用 IP）から出るので、WINTICKET の PRO 予想も取得できる見込み。

## 初回セットアップ

### 0. Termux を入れる
- **Play ストア版は古くて使えない**。F-Droid アプリから「Termux」を入れる（F-Droid 自体は公式サイト f-droid.org から）。
- 端末の空き容量を **3GB 以上** 空けておく（Ubuntu + ブラウザで約 2GB 使う）。

### 1. Termux で Ubuntu を用意する
Termux を開いて、この 2 行を実行（数分かかる）:
```
pkg update -y && pkg install -y proot-distro
proot-distro install ubuntu
```

### 2. ファイルを置く
このリポジトリの以下のファイルを、端末の「ダウンロード」フォルダに入れておく（チャットで送られたものと同じ）:
- pinpoint.py / daily_update.py / backfill_midnight.py / backfill.py
- android/setup_android_ubuntu.sh

Termux でこの 3 行を実行（ダウンロードフォルダから ~/keirin へコピー）:
```
termux-setup-storage
mkdir -p ~/keirin
cp ~/storage/downloads/pinpoint.py ~/storage/downloads/daily_update.py ~/storage/downloads/backfill_midnight.py ~/storage/downloads/backfill.py ~/storage/downloads/setup_android_ubuntu.sh ~/keirin/
```
※ `termux-setup-storage` の初回は、ストレージ許可のダイアログが出るので許可する。

### 3. Ubuntu の中でセットアップ（1 回だけ）
```
proot-distro login ubuntu -- bash /data/data/com.termux/files/home/keirin/setup_android_ubuntu.sh
```
apt とブラウザのダウンロードで **10〜20 分** かかることがある。終わると「🎉 セットアップ完了」が出る。

### 4. 接続 URL を書く（1 回だけ）
Ubuntu の中で env.sh を開いて、`DATABASE_URL=` の後ろに Mac で使っているものと同じ接続 URL を貼って保存する:
```
proot-distro login ubuntu
nano /data/data/com.termux/files/home/keirin/env.sh
```
（nano の保存は Ctrl+O → Enter、終了は Ctrl+X。終わったら `exit` で Termux に戻る）

## ふだんの実行（手動）
Termux でこれだけ:
```
bash ~/keirin/run_pinpoint.sh
```
- PRO 予想の取得: `run_pinpoint.sh`
- 出走表・結果の全取得: `bash ~/keirin/run_daily.sh`
- 前日分の回収: `bash ~/keirin/run_daily_yesterday.sh`
- ミッドナイト補完: `bash ~/keirin/run_backfill.sh`

実行には数分〜十数分かかる。**実行中は Termux を閉じない**こと（閉じると途中で止まる）。

## うまくいかないとき
- 「Unsupported platform」と出る → Termux 側で直接 python を実行してしまった合図。必ず proot の Ubuntu 経由（run_*.sh）で実行する。
- ブラウザの起動エラー → 空き容量を確認し、セットアップ（手順 3）をもう一度実行する。
- 途中で止まる → 画面を付けたまま Termux を開いて再実行する。pinpoint は同じレースを取り直しても安全（上書き方式）。
- 端末の電池最適化で Termux が止められる機種では、設定 → アプリ → Termux → バッテリー を「制限なし」にすると安定する。
