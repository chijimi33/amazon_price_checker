# Amazon price checker

PCパーツのセール監視を行うChatGPTタスク向けに、Amazon.co.jpの情報取得を補助するCLIです。人が管理する品質カタログから監視候補を選び、Amazon公式商品ページの確認値を優先して、タスクが扱いやすいJSONへ正規化します。ちもろぐ公開データとAmazon Creators APIはいずれも任意の候補発見・補助経路です。

## 基本フロー

```text
品質カタログ／任意の候補ソース
              ↓ ASIN候補
Amazon公式商品ページをPlaywrightで確認
              ↓ 確認JSON
価格・ポイント・品質条件・年内履歴を正規化
              ↓
他店比較を含むChatGPTタスクの通知判定
```

品質カタログとAmazon公式商品ページだけで運用できます。ちもろぐやCreators APIが利用できないことを理由に、Amazonページ確認を失敗扱いにはしません。

## Amazon公式商品ページをPlaywrightで確認する

Playwright CLIはグローバルインストールせず、このリポジトリ内へ入れます。`sudo` は不要です。

```bash
npm install
npm run browser:install
```

ASINまたはAmazon商品URLを最大10件ずつ確認します。

```bash
python3 amazon_browser.py \
  B0XXXXXXXXX \
  --output amazon_page_confirmations.json
```

品質カタログの検索結果や任意の候補JSONに `items`、`products`、`asins` のいずれかが含まれていれば、そのまま入力できます。

```bash
python3 amazon_browser.py \
  --candidates-file output/catalog_candidates.json \
  --max-items 10 \
  --output amazon_page_confirmations.json
```

このブラウザ補助は次の制限を守ります。

- 匿名の一時セッションを使い、Amazonへログインしない
- カート、購入、クーポン選択、注文確定ボタンをクリックしない
- CAPTCHAを検出したら回避・自動解答せず `source_status=error` と失敗範囲を出力する
- Amazon以外の販売元は `seller_trusted=null` とし、人の確認なしに不適格・適格を確定しない
- 送料を確認できない場合は0円と仮定せず、支払額と実質価格を未確定にする
- スナップショットなどの一時成果物は `output/playwright/` に保存する

出力は [amazon_page_confirmations.json.example](amazon_page_confirmations.json.example) と互換です。そのまま価格正規化へ渡せます。

```bash
python3 amazon_price_checker.py B0XXXXXXXXX \
  --confirmation-file amazon_page_confirmations.json \
  --confirmation-only \
  --quality-catalog catalog/quality_catalog.json \
  --history-file data/amazon_history.jsonl \
  --output output/amazon_products.json
```

Playwrightで確認できるのは「その匿名セッションの商品ページに表示された事実」です。Prime会員価格、ログイン後の個別ポイント、レジで確定する割引、配送先依存の送料・納期は未確定のまま残る場合があります。

## ちもろぐから初期候補を取り込む（任意）

品質カタログの初期作成や補助候補の発見に限り、Python標準ライブラリで次の公開ソースを取得できます。

- `https://chimolog.co/wp-content/price/`
- `https://chimolog.co/wp-content/price/data/products.json`
- `https://raw.githubusercontent.com/chijimi33/Chimolog-price-tool/main/public/chimolog_products.json`

PCパーツ関連候補を取得します。`--category` は複数回指定できます。

```bash
python3 chimolog_source.py \
  --category PCパーツ \
  --category SSD \
  --category グラボ \
  --category モニター \
  --discounted-only \
  --output data/chimolog_candidates.json
```

ちもろぐ本体JSONとGitHub正規化JSONの価格、ポイント、商品取得日時が異なる場合は、両方の値と差分を `items[].source_comparison` に残します。候補には、より新しいちもろぐ本体JSONを採用します。

通常モードでは、いずれかの取得に失敗しても品質カタログやAmazon確認を止めません。取得できた範囲と失敗ソースを `source_status`、`errors`、`source_calls` に残します。

この3ソースを特定の監視タスクで必須扱いにしたい場合だけ `--strict-sources` を指定します。

```bash
python3 chimolog_source.py --strict-sources --output data/chimolog_candidates.json
```

厳格モードで取得に失敗した場合は、空出力にせず次を設定します。

- `source_status=partial` または `error`
- `required_source_failure=true`
- `monitor_status_hint=監視エラー`
- `errors` と `source_calls` に失敗ソースと確認範囲

候補を共通Amazon形式へ変換します。

```bash
python3 amazon_price_checker.py \
  --chimolog-file data/chimolog_candidates.json \
  --confirmation-file amazon_page_confirmations.json \
  --quality-catalog catalog/quality_catalog.json \
  --history-file data/amazon_history.jsonl \
  --output output/amazon_products.json
```

商品ページ確認値がない価格は次のように明示されます。

```text
payment.reference_only = true
source_comparison.adopted_source = chimolog_public_json_reference
verification.status = amazon_verification_candidate
```

この参考価格は年内価格履歴へ書き込みません。Amazon商品ページで価格を確認した商品だけを履歴化します。

## 監視と製品探しで共有する品質カタログ

`catalog/quality_catalog.xlsx` を人間が編集する正本、`catalog/quality_catalog.json` をツールが読む生成物として扱います。価格監視専用のホワイトリストではなく、普段の製品探し、候補比較、Amazon取得結果の自動判定で共有する独立した製品データベースです。

```text
品質カタログ ──→ 条件検索・候補比較
      │
      └──────→ Amazonなどの価格観測へ品質判定を付加

価格・在庫履歴 ─→ 年最安値判定（品質カタログには保存しない）
```

データを次の単位に分けています。

- `products`: 型番、ASIN、JAN、価格.com ID、仕様、品質評価、弱点、リスク、根拠URL
- `profiles`: 「PCIe 4.0・2TB SSD」「27型QHD高リフレッシュ」など用途別の必須条件と加点条件
- `settings.default_quality_gate`: 承認状態、最低Tier、根拠数、評価期限、禁止リスク
- 価格、ポイント、送料、在庫: Amazon補助出力や価格履歴へ保存し、品質カタログから分離

同じSSDでも「ゲーム保存用」と「大量書き込み用」でプロファイルを変えられます。製品情報や根拠を重複登録する必要はありません。

### カタログの検証

```bash
python3 catalog_excel.py validate
python3 catalog_excel.py build
python3 catalog_excel.py check
```

`validate` はExcelの列定義、型、必須値、参照関係と生成予定JSONを検証します。`build` は検証成功後だけ `catalog/quality_catalog.json` を更新し、`check` はExcelと既存JSONの同期状態を確認します。JSONを直接編集せず、Excelから一方向に生成してください。生成後のJSON自体は従来どおり `python3 quality_catalog.py validate` でも検証できます。

Excelはカテゴリ別に `CPU`、`GPUChips`、`GPU`、`Memory`、`SSD`、`PSU`、`Motherboard`、`Monitor` シートを持ちます。`GPUChips` はGPUチップの性能・VRAM、`GPU` はボードメーカー別の完全SKUと品質を管理します。主要ASIN・JAN・価格.com ID・部品番号と、人間向けの品質要約は販売製品側のカテゴリシートへまとめています。追加識別子、根拠資料、既知の問題はそれぞれ `Identifiers`、`Evidence`、`Risks` に1件1行で登録します。

`CPU` シートには、デスクトップ向けRyzen 5000シリーズ以降とIntel Core第12世代以降（Core Ultra 200Sを含む）の主要製品を初期登録しています。公式の発売日または発売時期、コア構成、アーキテクチャに加え、PassMarkの `CPU Mark` と `Single Thread Rating` を確認日付きで保持します。PassMark値は継続的に変動する参考指標なので、根拠行のURLと `passmark_checked_at` をセットで更新してください。日単位の発売日を公式資料で確定できない製品は `release_date` を空欄にし、`launch_period` と `release_date_precision` に四半期または月の精度を記録します。

CPUの初期行はすべて `research_required` / `unrated` です。性能値が登録済みでも、ASIN・JAN、国内リテール/OEM区分、保証、独立レビューを確認するまでは自動監視の承認対象になりません。

`GPUChips` にはデスクトップ向けのGeForce RTX 20/30/40/50シリーズ38構成と、Radeon RX 6000/7000/9000シリーズ25構成を初期登録しています。VRAM違いは別IDとし、世代、アーキテクチャ、発売日または発売時期、VRAM、メモリバス、PassMark G3D/G2Dと確認日を保持します。モバイル、ワークステーション、OEM専用、地域限定型番は初期対象外です。チップ行は性能比較用の参照レコードであり、販売商品の品質承認を意味しません。

グラフィックボードは `GPU` シートへ完全な部品番号単位で登録し、`gpu_chip_id` で `GPUChips.product_id` を参照します。ExcelからJSONを生成すると、参照先のVRAM・世代・PassMark値が `specs.gpu_chip` へ展開されます。ボード側ではメーカー（`brand`）、シリーズ、リビジョン、クーラー設計、ファン数、騒音、カード長、占有スロット、補助電源、国内代理店、保証を個別評価します。同じシリーズ名でも世代やリビジョンをまたいで品質を自動継承しません。

仕様項目は後から追加できます。

1. カテゴリシートのExcelテーブル内へ新しい列を追加する
2. `FieldDefinitions` に同じ `sheet_name` と `column_name` を追加する
3. `json_path` を `specs.追加項目名`、`data_type` を適切な型にする
4. `python3 catalog_excel.py validate` と `build` を実行する

`FieldDefinitions` が列名からJSONパスと型を解決するため、`specs` 配下の項目追加ではPythonコードやJSON Schemaの変更は不要です。未定義列や定義だけ存在する列は検証エラーになり、入力の取りこぼしを防ぎます。カテゴリ自体を増やす場合は `SheetDefinitions` にも1行追加します。

JSON Schemaは `catalog/quality_catalog.schema.json` にあります。製品レコードの記入例は `catalog/example_product.json` です。記入例は実在製品ではないため、そのままカタログへ登録しないでください。

品質状態は次のように使います。

- `research_required`: 仕様または独立レビューの確認途中
- `approved`: 品質条件を満たし、自動監視の候補にできる
- `preferred`: 同クラス内で積極的に探したい
- `rejected`: 既知の問題や用途不一致により除外
- `discontinued`: 終売品。履歴参照用に残す

`approved` と `preferred` でも、根拠不足、評価期限切れ、禁止リスクがあれば自動判定では不適格になります。

### 自分で製品を探す

用途プロファイルを一覧表示します。

```bash
python3 quality_catalog.py profiles
python3 quality_catalog.py profiles --category ssd
```

用途条件をすべて満たし、品質ゲートを通過した製品をスコア順に表示します。

```bash
python3 quality_catalog.py search --profile ssd-pcie4-balanced-2tb
python3 quality_catalog.py search --profile monitor-qhd27-high-refresh
```

調査途中や除外された製品も含め、落ちた理由を確認できます。

```bash
python3 quality_catalog.py search \
  --profile ssd-pcie4-balanced-2tb \
  --include-ineligible
```

任意の仕様条件を追加できます。値はJSONとして解釈されます。

```bash
python3 quality_catalog.py search \
  --category ssd \
  --where specs.capacity_gb:gte:2000 \
  --where specs.nand_type:eq:TLC
```

1製品について、必須条件、加点条件、品質ゲートを個別に確認します。

```bash
python3 quality_catalog.py evaluate PRODUCT_ID \
  --profile ssd-pcie4-balanced-2tb
```

### Amazonの観測結果へ品質判定を付ける

```bash
python3 amazon_price_checker.py B0XXXXXXXXX \
  --quality-catalog catalog/quality_catalog.json \
  --quality-profile ssd-pcie4-balanced-2tb \
  --history-file data/amazon_history.jsonl \
  --output output/amazon_products.json
```

ASIN、部品番号、完全なモデル名の順でカタログと照合します。短い型番の部分一致だけでは自動承認しません。

Amazon出力の `products[].quality_catalog` には次が追加されます。

- `match_status`: `matched`、`not_cataloged`、`ambiguous`
- `quality_gate`: 承認状態、Tier、根拠数、期限、リスクの判定
- `profile_evaluations`: 必須条件と加点条件の詳細
- `automation_eligible`: 価格通知の母集団に含めてよいか

未登録製品、曖昧一致、品質未評価品は、価格が安くても `automation_eligible=false` になります。候補自体は削除しないため、後から品質調査してカタログへ追加できます。

## このツールが確認する項目

- ASIN指定商品の現在価格、ポイント、販売元、在庫区分、状態
- タイムセール種別、Prime限定セール、セール期限、割引率
- 商品名、ブランド、型番、部品番号、保証
- Ryzen、SSD、モニター、電源などのキーワード検索による候補発見
- 支払額、ポイント、ポイント込み実質価格の分離
- 同一ASIN・同一状態について、このツールが2026年中に確認した価格履歴との比較
- Creators APIまたは任意の公開候補価格と、Amazon公式商品ページ確認値の差異

Creators APIにない「商品ページクーポン」「レジ割引」「発送元」「カート投入可否」は推測しません。Playwrightでも確認できなかった項目は `amazon_verification_candidate` として出力され、未確認項目と購入前確認の必要性が明記されます。

## Creators APIを任意で使う場合

旧Product Advertising APIは2026年5月15日に廃止されました。API経路を選ぶ場合は、後継のCreators APIと公式Python SDK 1.2.0を使います。CAPTCHA回避やログインCookieの収集は行いません。

Creators APIの利用にはAmazonアソシエイト参加、API登録、認証情報、対象マーケットプレイスで直近30日以内に10件以上の適格販売など、Amazonが定める条件があります。取得したコンテンツの利用・保存・公開もCreators APIおよびアソシエイト規約に従ってください。

- [Creators API公式ドキュメント](https://affiliate-program.amazon.com/creatorsapi/docs/)
- [Amazon公式Python SDK](https://pypi.org/project/amazon-creatorsapi-python-sdk/)

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

認証情報はファイルやコマンドライン引数に書かず、環境変数で渡します。

```bash
export AMAZON_CREATORS_CREDENTIAL_ID="..."
export AMAZON_CREATORS_CREDENTIAL_SECRET="..."
export AMAZON_CREATORS_VERSION="2.3"
export AMAZON_CREATORS_PARTNER_TAG="example-22"
```

`AMAZON_CREATORS_VERSION` は認証方式・リージョンにより異なります。公式SDKの例ではFar Eastに `2.3`（Cognito）または `3.3`（LWA）が示されているため、Associates Centralで発行された値を使用してください。

## 使い方

ASINまたは商品URLを再確認します。

```bash
python3 amazon_price_checker.py B0XXXXXXXXX \
  --history-file data/amazon_history.jsonl \
  --output output/amazon_products.json
```

カテゴリー候補を検索する場合は、設定例をコピーして検索語を調整します。

```bash
cp config.json.example config.json
python3 amazon_price_checker.py \
  --config config.json \
  --history-file data/amazon_history.jsonl \
  --output output/amazon_products.json
```

`config.json` の主な項目は次の通りです。

```json
{
  "marketplace": "www.amazon.co.jp",
  "asins": ["B0XXXXXXXXX"],
  "searches": [
    {
      "id": "ssd",
      "keywords": "NVMe SSD 2TB",
      "search_index": "Computers",
      "item_count": 10,
      "min_saving_percent": 10
    }
  ]
}
```

検索結果だけで今年最安値を確定しないでください。`year_low_reference` はこのツールが保存した同一ASIN・同一状態の履歴だけを比較する参考値で、他店やAmazonの全期間履歴を表すものではありません。

## Amazon商品ページ確認を優先する

候補のAmazon公式商品ページで確認できた項目を `amazon_page_confirmations.json` に記録します。Playwrightの出力をそのまま利用でき、人が追加確認した値も同じ書式へ追記できます。書式は [amazon_page_confirmations.json.example](amazon_page_confirmations.json.example) を参照してください。

```bash
cp amazon_page_confirmations.json.example amazon_page_confirmations.json
python3 amazon_price_checker.py \
  B0XXXXXXXXX \
  --confirmation-file amazon_page_confirmations.json \
  --confirmation-only \
  --history-file data/amazon_history.jsonl \
  --output output/amazon_products.json
```

`--confirmation-only` を外すと、確認値をCreators APIの結果と比較するモードになります。

計算ルールは次の通りです。

```text
支払額 = 採用価格 - 適用済みクーポン - 適用済みレジ割引 + 送料
実質価格 = 支払額 - 獲得予定ポイント
```

- 商品ページの `price_yen` と `points_yen` があればAPI値より優先します。
- Prime会員として確認済みの場合だけ `prime_exclusive_price_yen` を採用します。
- クーポンとレジ割引は `*_applied: true` のときだけ支払額から引きます。
- 送料未確認の公式ページ価格からは支払額・実質価格を計算しません。
- API値と商品ページ値、双方の確認時刻、採用元、差額は `source_comparison` に残ります。
- 在庫切れ、カート投入不可、値上がり、販売元不適格、型番・状態不一致は `verification.exclusion_reasons` に残ります。

## 取得失敗時

認証エラー、API失敗、ASIN欠落、履歴破損があっても空出力にはしません。JSONの `source_status` は次のいずれかです。

- `ok`: 必須取得呼び出しに成功
- `partial`: 一部呼び出しまたは一部ASINが失敗
- `error`: 商品を1件も取得できず、エラーがある

`source_status=error` の場合、ChatGPTタスクは空応答にせず、先頭を「監視エラー」とし、`errors` と `source_calls` から失敗ソースと確認できた範囲を記載してください。このJSON自体はAmazon補助入力であり、楽天市場、Yahoo!ショッピング、ドスパラ、ツクモ、パソコン工房との比較後に最終通知を判定します。

認証情報がない環境でも、保存済みのCreators APIレスポンスを使って正規化ロジックを確認できます。

```bash
python3 amazon_price_checker.py \
  --response-file creators_response.json \
  --no-history-write
```

## テスト

```bash
python3 -m unittest discover -s tests -v
```

テストは公式APIへ接続せず、価格優先順位、実質価格、要確認候補、除外理由、非空エラー、年内履歴を検証します。
