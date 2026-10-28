<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo アルゴ：Agent のための検索と深掘り調査ツール、284 の検索ソース、ドメインと言語で経路を選び、吸収できる証拠を出力">
</p>

<p align="center">
  <a href="README.md">中文</a> ·
  <a href="README.en.md">English</a> ·
  <strong>日本語</strong> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.es.md">Español</a>
</p>

<p align="center">
  <a href="#これは何か">紹介</a> ·
  <a href="#モデル内蔵検索--ai-検索--横断検索より強い点">比較</a> ·
  <a href="#聞き方に合わせたルーティング">証明</a> ·
  <a href="#仕組み">仕組み</a> ·
  <a href="#クイックスタート">導入</a> ·
  <a href="#できること">機能</a> ·
  <a href="#インストールと設定">設定</a> ·
  <a href="#最近の更新">更新</a>
</p>

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-Apache_2.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.9.9-informational">
  <img alt="engines" src="https://img.shields.io/badge/engines-284-orange">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-19%20tools-purple">
</p>

> **これは taxueseek の DeepSeek Harness プラグイン群のひとつ**（[dsh-files](https://github.com/taxueseek/dsh-files) 39★）。同作者のスキルシリーズ：[session-digger](https://github.com/taxueseek/session-digger) 29★ · [taxue-invest](https://github.com/taxueseek/taxue-invest) 21★ · [taxueskills](https://github.com/taxueseek/taxueskills) 23★ · [taxue-creative-style](https://github.com/taxueseek/taxue-creative-style) 12★

# Argo アルゴ：Agent のための検索と深掘り調査ツール

## これは何か

**Argo は Agent に組み込んで使う検索・深掘り調査ツールで、関連するウェブページの情報も取得できます。背後には 284 の検索ソースがあり、99 の業務ドメインをカバーし、ローカルファイルの基本的な検索も備えています。オープンソースの検索プロジェクトの中では、非常に強力な検索能力を持っています。**

API Key を設定すれば検索品質を上げられます（Key を設定したソースは品質が良いことが多く、未設定なら自動でスキップ）。無料エンジンとローカルエンジンによる自動バックストップにも対応しています。中英日韓など 18 言語をカバーし、金融・学術・映像・スポーツ・コード・マクロなどの垂直シーンに対応します。

## サブスキル

Argo の能力は四つのサブスキル（sub-skills）で構成され、各サブスキルは独立して使えるパイプラインです：

| サブスキル | 能力 |
|--------|------|
| **local-search** | ローカルファイル検索 + ローカルゼロコスト集約エンジン（32 のローカルエンジン）。コードシンボル、ファイル名、ドキュメント全文、ファイル構造を検索できます。ネット検索結果に統合でき、スイッチひとつでローカルのヒットを同じ検索に混ぜられます |
| **local-seek** | ローカル意味検索（BM25 + ベクトルのハイブリッド）で、ノート・記憶・資料庫を検索。三層の段階：まずファイルを特定し、次に文脈を見て、最後に段落を精読します |
| **ego-search** | ログイン状態での専門検索。知乎、小红书、公众号などログインしないと見られない内容を、実ブラウザとログイン状態で検索します。既定はオフで、必要なときに有効化します |
| **js-run** | ブラウザなしでウェブ JS を実行。ブラウザを開かずに「環境探査 + 純計算」型のウェブスクリプトを走らせ、1 回あたりのコンテキストは P50 約 1ms |

四つのサブスキルは「ローカルを検索する」から「ウェブ全体を検索する」、さらに「ログインウォールの向こうを検索する」までをカバーします。ほとんどの場面は local-search + ネット検索で足り、特殊な場面で ego-search と js-run を有効にします。

## DSH プラグイン

Argo は DeepSeek Harness のプラグインでもあり、一行のコマンドでインストールできます：

```bash
dsh plugin --profile web add argo-search                  # npm リリース版
dsh plugin --profile web add "github:taxueseek/argo"      # GitHub main
```

インストール後に `dsh web` を再起動すれば有効になります。検索と取得がネイティブの一等ツールになり、MCP 接続に依存せず、常駐コンテキストのオーバーヘッドもありません。ほかに `wide_research` の並列リサーチが 1 つ、MCP ツールが 19 あり、必要に応じて有効化できます。接続形態の全体像は後述します。

### 3 つの呼び出し経路、1 つの能力セット

同じ argo 検索コア（同じエンジン、同じ圧縮、同じガード）が DSH 内で 3 つの経路を開き、ホストの能力に応じて自動的に降格し、互いに冗長性を担保します：

| 経路 | 既定 | 内容 |
|------|------|------|
| **ネイティブ一等ツール** | オン | `argo_search` / `argo_fetch` などを `ctx.tools.register` で登録し、CLI 単発（`--call`）として実行。MCP と同じエンジンとガードのスタックを再利用するため、**MCP 接続不要・常駐トークン開銷ゼロ** |
| **ネイティブ web seam** | オン | `argo` を検索プロバイダと取得プロバイダの両方として登録し、ホスト内蔵の `web_search` を argo のエンジンチェーンへ、`web_fetch` を argo の取得チェーンへルーティング（HTTP 優先、Cloudflare / JS 描画では自動でアンチ検出ブラウザへ昇格） |
| **MCP 形態** | 必要時 | 19 個の `mcp__argo__*` 完全ツール面。ツール定義は毎ラウンドコンテキストに注入され圧縮でも消えません（19 ツールで実測 ≈3.7K トークン/ラウンド）。一方で高頻度パスはネイティブツールと web seam が既にカバーしているため、既定ではマウントしません。長尾のフォレンジックツールが必要なときは profile patch の `mcp-argo` 行コメントを外します |

ほかに `wide_research` が 1 つあります：補完的なトラックを計画（`depends_on` で段階化、既定は並列）→ 有界並行のサブエージェントで証拠収集 → ソース台帳（http(s) URL のみ記録）→ 統合レポート。出力には `quality_gate_results`（`conclusion_cap`: low/medium/high）が付き、`passed=false` のときは表現を弱め、結論を出す前に台帳のソースを検証します。worker の証拠ツール許可リストは MCP 名とネイティブ名の両方を書き（allow セマンティクス）、MCP マウントの有無どちらでも動作します。MCP があれば完全なツール面を使い、なければ `argo_search` / `argo_fetch` とホストの `web_search` / `web_fetch` に落ちます。

日常的な質問には `argo_search`、単一エージェントのフォレンジックには `argo_research`、引用台帳付きの多角的比較には `wide_research` を使い分けます。worker は既定では argo の証拠ツールしか呼べず、`argo_research` を再度呼ぶことはできません（ハードガード——ユーザー層での放行も無効）。

### 内蔵設定ページ：6 分区、保存即有効

プラグインにはブラウザ側の設定ページが同梱され、入口は 2 つあります：設定ポップアップ（`Cmd+,`）の左メニュー項目「Argo」、または 設定 > プラグイン > Argo（同じパネルの概要カード、クリックで開きます）。

<p align="center">
  <img src="assets/readme/settings-tools.webp" width="100%" alt="Argo 設定ページ「ツール」：ネイティブ 18 ツールのチェック——高頻度パスは argo_search / argo_fetch が既定でオン、長尾 16 個は必要時に。下に単発タイムアウトと返却上限">
</p>

<p align="center">
  <img src="assets/readme/settings-engines.webp" width="100%" alt="Argo 設定ページ「エンジン」：argo 本体の 28 個の挙動スイッチをパネルへ——キャッシュ / TF-IDF 再ランク / 数値クロスチェック / MinHash 重複排除 / SERP ガードと重複しきい値。未設定行は既定値、保存で即時有効">
</p>

<p align="center">
  <img src="assets/readme/settings-keys.webp" width="100%" alt="Argo 設定ページ「キー」：DeepSeek 公式検索は「設定済み・資格情報ストア」で、ホスト web_search と同一キーを共用。Tencent Yuanbao / Tavily などのキー入力・更新・クリア、平文は非表示">
</p>

| 分区 | 管理内容 |
|------|----------|
| **接入** | `web_search` / `web_fetch` seam の切り替え。エンジン起動コマンドと引数（npx またはローカル `mcp_server.py`）。検索/取得のタイムアウト、本文上限、アイドル接続回収、並行接続数 |
| **ツール** | 18 個のネイティブ一等ツールのチェック（既定は `argo_search` / `argo_fetch` のみ）。単発呼び出しタイムアウトと返却テキスト上限 |
| **引擎** | argo 本体の挙動スイッチ（従来は環境変数、curated ホワイトリスト 28 個：キャッシュ / TF-IDF 再順位 / 数値交差検証 / MinHash 重複除去 / SERP ガード、Jina / モバイル UA / Parallel 取得層、意味的証拠層、ヘッジ幅と末尾幅、エンジン有効化リスト、プロキシ、ローカル検索ルートと読み取り許可リスト、ウォームアップ / 使用ログ / 接続プール / 各種 TTL）。スイッチ系は即時反映、数値・テキスト・リスト系はグループ保存。未設定行は argo の既定値を表示し「默认」と明記。認可系スイッチ（再計算実行、イントラネット URL）は意図的にパネルに入れていません |
| **密钥** | 常用エンジン key の入力・更新・クリア。「設定済み / 未設定 + 来源層（凭证库 / 起動環境 / .env）+ 読み取り専用状態」を表示し、平文は非表示。各行の「転送」チェックは参照名を `keyRefs` に追加するのと等価 |
| **研究** | worker / トラック / トラックごとのソース数 / トークン上限とレポート出力先ディレクトリ |
| **高级** | worker ツールの allow / deny リスト、凭证転送リスト |

保存は即時有効です（すべて volatile 設定）：ネイティブツール群はチェックに応じて登録・解除され、タイムアウト・並行数・研究上限は次の呼び出し時に読み込まれます。エンジンスイッチは `argoEnv`（1 行 1 つの `KEY=VALUE`）として argo サブプロセスの env に入り、次の新規接続から有効になります（プール内の旧接続は `searchIdleMs` で回収、既定 60 秒）。構造的フィールド（`toolName`、`provider`、`searchProviderId`、`fetchProviderId`）は設定ページにはなく、従来どおり profile patch 経由で、変更後は再起動が必要です。

**キーについての安全設計**：キーは DSH の凭证库（Models ページと同じ場所）に書き込まれ、プラグイン設定には書かれず、ログにも残らず、平文も表示しません。凭证の解決優先順位は**ストレージファイル層 > 起動環境 > プロジェクト .env > ホーム .env**で、パネル（または Models ページ）で保存すると起動環境の古い値を上書きし、argo サブプロセスは次の新規接続で新しい key を受け取ります。「クリア」は凭证库の値だけを削除するため、環境変数や `.env` が供給した key は再び現れます。設定ページは凭证ドメインの書き込み入口であり、読み取りはホストが毎回の呼び出しで行います。

**DeepSeek 公式 key はネイティブに供給**：argo の `deepseek_search` エンジン（公式 `api.deepseek.com/v1/tools/search`、結果に authority 権威分級 / rerank_score / 公開時刻が付く）は DSH 公式検索バックエンドと**同じ key を共有**します。プラグインは DSH のネイティブ凭证 seam 経由で取得するため、手動設定は不要です。解決は `ctx.get('credentials').resolve('DEEPSEEK_API_KEY')` で、呼び出しごとに解決されます。web Models ページで key をローテーションすれば、次の新規接続で反映され、プラグインの再起動も不要です。プラグインは key ファイルを読まず、ディスクに書かず、ログにも残しません。key の値はメモリ経由でのみ argo サブプロセスの env に入り、出力やエラーメッセージに一切含まれません。credentials サービスを持たない旧ホストでは静かにスキップし、そのエンジンは key 不足で無効化されますが、他の 263 ソースは影響を受けません。

**web seam の pin は必読です**（設定漏れは `web_fetch` を壊します）：ホスト `dsh-web` のプロバイダ選択ルールは「設定 id が優先。未設定で利用可能がちょうど 1 つならそれを選ぶ。**複数ある場合は `WEB_PROVIDER_AMBIGUOUS` で失敗**」。web profile は既定で公式 http fetch プロバイダ（key 不要・常に利用可能）を保持し、このバンドルがもう 1 つ登録するため、pin が無いと利用可能が 2 つになり、**`web_fetch` がまるごと壊れます**（profile に別の検索プロバイダがある場合、`web_search` も同様）。したがって web seam を有効にする profile は、必ず web サービス行で明示的に指定してください：

```yaml
- id: web
  config:
    searchProvider: argo    # 検索 seam が有効な場合（既定で有効）に必須
    fetchProvider: argo     # 取得 seam が有効な場合（既定で有効）に必須
```

`searchProviderEnabled: false` / `fetchProviderEnabled: false` は対応する seam をまるごと無効にします（プロバイダは登録されたままだが選択不可、もう一方には影響しません）。エンジン側では、Firecrawl などの匿名利用可能なエンジンは **key 不要でそのまま使えます**（無料枠）。キーが設定されたエンジンは自動的に認証リクエストへ昇格し、キー不足はルーティングを止めません。

> 設定項目の全体、Compatibility バージョン対応表、アンインストール手順は [packages/dsh-plugin/README.md](packages/dsh-plugin/README.md) にあります。

## 主な強み

**284 のソース、うち 241 はキー不要ですぐ使える。** API Key を一切設定しなくても動き、無料エンジンとローカルエンジンが自動でバックストップします。Key を設定したソースは品質が良いことが多く、未設定なら自動でスキップされます。一度インストールすれば、あらゆる場面で使えます。

**ローカル検索とネット検索が一体。** これは Argo ならではの能力です。ローカルファイル検索の土台は rg や mdfind といった開発者が日常的に使うツールで、コードシンボル、ファイル名、ドキュメント全文、ファイル構造を検索します。スイッチひとつで、ローカルのヒットが同じネット検索結果に混ざり、結果には出所がローカルファイルであることが表示されます。半年ほど前に書いた一節を思い出したが場所がわからない、というときに、フォルダを巡回するより速く見つかります。

**二層キャッシュで、ホットクエリは約 10ms。** 鮮度がそれほど重要でない内容（百科事典系、過去データなど）は、一度検索するとキャッシュに入り、同じクエリはそのままヒットして毎回 API を叩きません。コストも時間も節約できます。

**多言語 + 垂直シーン。** 実測で 18 言語に対応し、中国語・英語・日本語は現地の一次ソースにたどり着けます。論文は学術ライブラリ、映像は項目ライブラリ、コードは Q&A コミュニティ、相場は金融ソースへ。曖昧な質問だけが汎用検索に回ります。

## インストールしてすぐ使う

設定ゼロで動きます。インストール方法は三つ、好みに合わせて選んでください：

**AI アシスタント自身に検索させたいなら**、スキルパッケージをそのスキルディレクトリに入れます。以後「ちょっと調べて」「事実確認して」と言えば、自分で検索します。

**気軽に試したいなら**、npx コマンド一行でその場で使えて、インストール不要です。

**アシスタントに常駐させたいなら**、MCP を載せれば 19 の検索ツールがいつでも使えます。ワンコマンド注入も用意しており、Claude Code、Cursor、Windsurf に自動で設定でき、Codex、OpenCode、Cline も対象です。設定を書く前にバックアップを取るので、いつでも戻せます。

```bash
# 方法 1：ワンコマンドスクリプト（本機で長期利用に推奨）
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash

# 方法 2：npm / npx（対応済み、現在 2.9.9。tag ごとに公開、バージョン固定）
npm install -g argo-search       # CLI をグローバルインストール
npx -y argo-search               # またはインストールせず即時利用
npx -y github:taxueseek/argo     # main の最新コミットに追従したい場合

# 方法 3：git clone（開発 / ソース変更）
git clone https://github.com/taxueseek/argo.git
```

インストール後に一つ試してみてください：

```bash
argo search "python asyncio error handling"
```

返ってくるのは、StackOverflow の公式 API からコミュニティスコア順に並べ、採用済みかどうかを示した質問です。SEO ファームの山ではありません。

自分のマシンで今いくつのソースが使えるか見たいときは、コマンド一つで一覧できます：

```bash
argo search --list-engines
```

## 接続形態

Argo は四つの接続方式をサポートします。場面に合わせて選んでください：

| 形態 | 向き | 説明 |
|------|------|------|
| **MCP Server** | Agent への常駐マウント | 19 の検索ツールがいつでも使え、コマンド一つで Claude Code / Cursor / Windsurf / Codex / OpenCode / Cline に注入。設定を書く前に自動バックアップ |
| **DSH プラグイン** | DeepSeek Harness ユーザー | `dsh plugin add` 一行でインストールでき、検索と取得がネイティブの一等ツールになる。MCP 接続に依存せず、常駐コンテキストのオーバーヘッドもない。保存即有効の 6 分区設定ページ同梱 |
| **Skill ファイル** | Claude Code / Grok Build / Kimi など | スキルパッケージを Agent のスキルディレクトリに入れる。「ちょっと調べて」「事実確認して」と言えば自分で検索する |
| **npx 即時利用** | さっと試したい | `npx -y argo-search`（npm リリース版）または `npx -y github:taxueseek/argo`（main）。インストール不要で、任意のクライアントに一時的に MCP を載せるのに向く |

四つの形態は土台が同じ Python コードで、CLI と MCP ツールは一対一に対応し、どれを入れても能力は欠けません。

## モデル内蔵検索 / AI 検索 / 横断検索より強い点

> 要するに：前三者は **人** が情報を探すためのもの。Argo は **Agent** が検索と検証を一本でやるためのもの。差は UI ではなく成果物——人向けの要約ページやリンク一覧対、Agent が並べ替え・再検証でき、文脈を膨らませない証拠。

<p align="center">
  <img src="assets/readme/why-better.svg" width="100%" alt="左：人向けの 3 種の既定検索。右：Agent が吸収できる Argo の証拠 JSON">
</p>

| 次元 | モデル内蔵検索 | AI 検索（要約型） | 横断検索 / 検索エンジン | **Argo** |
|------|----------------|-------------------|-------------------------|----------|
| 結果の形 | つなぎ合わせた長文 | 人向け要約ページ | SERP リンク一覧 | **コンパクト JSON：証拠候補 + 信頼度分解** |
| 垂直の問い（相場 / 化学式） | 汎用ウェブ | 汎用ウェブして要約 | 汎用ウェブ | **垂直ソース直結、答え型** |
| 証拠の信頼度 | スコアなし | 構造化スコアなし | スコアなし | **selection · absorption · freshness · 合意** |
| 同じクエリ | 毎回ネットワーク | 毎回ネットワーク | ページキャッシュ | **二層キャッシュ（メモリ + SQLite）、ホットクエリ約 10ms** |
| コスト制御 | 制御不能 | 1 回が高い | 無料だが手間 | **予算モード、無料優先、Key はすべて任意** |
| 多言語 | モデル任せ | モデル任せ | エンジン任せ | **言語検出 + エンジン言語パラメータ + 多言語ルーティング** |

> 仕組みとして Argo は検索を **証拠パイプライン** として扱う：言語検出 → ドメイン経路 → 多エンジン召回 → RRF 融合 → 証拠速評。Agent が並べ替え、`fetch` で核验し、文脈に収まる材料を返す。

---

## 2026 年、検索で何が変わったか

1. **リンクから証拠へ。** エージェントには並べ替え可能・検証可能・コンテキストに収まる構造化データが必要 — Argo は信頼度内訳付き JSON を返す。
2. **コンテキストが第一コスト。** エージェントプロファイルは 1 回約 3.7KB、フィールドとバイト予算はゲートで固定。
3. **サイトは AI 向け資料を備え始めた。** llms.txt と `.md` 直出しが主要ドキュメントサイトに普及 — フェッチチェーンはレベル 0 で自動探知、r.jina.ai リーダーもfallback。
4. **無料オープン生態系で足りる。** 政府・学術・標準・セキュリティの公開 API とキーレス エンジンで大部分をカバー（241 ソースが無設定）。
5. **品質は測定可能。** ランキング金標・融合利得アブレーション・負のルーティング制御。

> 以上の 5 項目はすでに v2.9.x 系で全て実装済み：284 ソース / 99 ドメイン / 241 無設定。

---

## 聞き方に合わせたルーティング

<p align="center">
  <img src="assets/readme/proof-routes.svg" width="100%" alt="4 つの実ルート：金融・映像・多言語・地理">
</p>

| こう聞くと | だいたいこう動く |
|------------|------------------|
| python asyncio error handling | プログラミング QA ドメイン → StackOverflow/StackExchange 公式 API、スコアと採答マーカー付き |
| AAPL / 米株プレマーケット | 米株ドメイン、A 株と分離 |
| 肖申克的救赎 主演 / Inception director | 映像ドメイン → IMDb など |
| 梅西 俱乐部 / 库里 球队 | スポーツ → TheSportsDB など |
| 埃菲尔铁塔在哪 / where is Eiffel Tower | 地理エンティティ → OpenStreetMap など |
| NASA founding year / 国务院职能 | 組織 → Wikidata など |
| 周杰伦 专辑 / Taylor Swift album | メディア → iTunes など |
| アニメ おすすめ / 한국 영화 추천 | 日/韓を検出 → 言語に合うソース、中国語専用サイトを避ける |
| 米国 CPI、日本 インフレ率 | マクロ；国別振り分け（自国の一次ソース優先） |
| log4j CVSS / nodejs 22 end of life | セキュリティ → NVD 公式；ライフサイクル → endoflife.date |
| attention is all you need | 学術 → arXiv/OpenAlex/CrossRef のメタデータと DOI リンク |
| 阿司匹林 分子式 | 化学 → PubChem 系 |
| 台積電バリュエーション議論（深掘り） | サブ問題分割 + 並列ソース、垂直を boost |

---

## 仕組み

<p align="center">
  <img src="assets/readme/workflow.svg" width="100%" alt="クエリ → 言語とドメイン → 多エンジン → RRF → 証拠 → 統一 JSON">
</p>

```
クエリ
  ├─ 意図の曖昧さ解消（任意）
  ├─ クエリ書き換え（任意；ルーティングは元の意図を見る）
  ├─ 言語検出 + 言語嗜好
  ├─ ルート（ドメイン規則 + TF-IDF + 予算 + 言語補完 + ホットパスキャッシュ）
  ├─ 多エンジン召回（ブレーカ / 負キャッシュ / 並列）
  ├─ 空結果の段階復旧（緩和 → 同族/汎用 → 言語横断；汚染防止）
  ├─ RRF 融合 + 任意リランク
  ├─ 証拠の速評（権威 · 密度 · 鮮度 · 合意）
  └─ 統一 JSON（engine_outcomes / recovery 含む）
```

### 証拠スコア（簡易）

```
selection  ≈ ドメイン権威。SERP / リダイレクト殻は大きく減点
absorption ≈ 数字 / 定義 / 比較 / 開示などの証拠密度
freshness  ≈ 公開時刻（「2015 年以来」のような歴史比較年は無視）
総合       ≈ 0.40·selection + 0.35·absorption + 0.15·freshness + 0.10·エンジン分
```

結果には `selection`、`absorption`、`credibility_fast`、`evidence_flags` などが付き、エージェントが直接並べ替えできます。

### エージェント利用の規律（推奨）

1. **高影響の問い**（ポジション、安全、真偽）：search → 速評を見る → top を `fetch` → 結論  
2. **数字**：计算方式（定義・範囲）を明示し、矛盾は並列表示、無理に合成しない  
3. **SERP / リダイレクト**：本文ソースにしない  
4. **SNS**：世論・物語として扱い、事実の真値にしない  
5. **ファクトチェック**：ソース / 比較 / 主体など層化したクエリを足す

---

## クイックスタート

どれか 1 つで十分です。**npm インストールに対応済みで、最新版は 2.9.9**。GitHub と同じく本リポジトリから公開しています。npm 経由は `npm install -g argo-search` または `npx -y argo-search`、tag ごとに公開され、バージョンが固定されダウンロードも小さい。GitHub 経由は `install.sh` / `install.ps1` または `npx github:taxueseek/argo` で main に追従。今後は tag を打つたびに CI が npm の 2 パッケージと GitHub Release をまとめて公開します。registry 上の古い v1.0.1 は履歴版なので、それに固定しないでください。

**ゼロ設定で動く**：API Key なしなら無料エンジン + ローカル `local_*`。Key があるソースは品質が上がりやすく、無いものは自動スキップ。

### 方法一：インストールスクリプト（本機長期利用向け）

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
```

ホームと Skill リンク指定：

```bash
curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh \
  | bash -s -- --home "$HOME/.local/share/argo" --link "$HOME/.claude/skills/argo"
```

確認：

```bash
python3 ~/.local/share/argo/scripts/search.py "python asyncio error handling" --json
python3 ~/.local/share/argo/scripts/search.py --list-engines
```

### 方法二：GitHub から MCP（エージェントへ即接続）

**Node.js 18+** と **Python 3.9+** が必要。初回：

```bash
pip3 install pyyaml
```

```bash
npx -y github:taxueseek/argo
```

クライアント設定例（Claude Code / Cursor / Kimi など）：

```json
{
  "mcpServers": {
    "argo": {
      "command": "npx",
      "args": ["-y", "github:taxueseek/argo"]
    }
  }
}
```

より安定・Node 不要：方法一で入れた後、ローカル Python を指す：

```json
{
  "mcpServers": {
    "argo": {
      "command": "python3",
      "args": ["/path/to/argo/scripts/mcp_server.py"]
    }
  }
}
```

Python が特殊なパスのとき：`export ARGO_PYTHON=/path/to/python3`（npx 入口のみ参照）。

### DeepSeek Harness 一行プラグイン

DeepSeek Harness では 2 通り：

```bash
# A：mcp__argo__* ツール 19 個（メインパッケージ bundle、MCP 全量と同じ）
dsh plugin --profile web add argo-search                  # npm リリース版
dsh plugin --profile web add "github:taxueseek/argo"      # GitHub main

# B：検索ツール + wide_research 並列研究オーケストレーション（サブパッケージ）
dsh plugin --profile web add @taxueseek/argo-dsh          # npm リリース版
dsh plugin --profile web add "github:taxueseek/argo#main&path:packages/dsh-plugin"
```

インストール後に `dsh web` を再起動。詳細は `packages/dsh-plugin/`。

### 方法三：Release ソースパッケージ

[Releases](https://github.com/taxueseek/argo/releases) から最新の **`argo-x.y.z.tar.gz`** を入手（`x.y.z` は最新リリース番号に読み替え）：

```bash
tar -xzf argo-x.y.z.tar.gz
cd argo-x.y.z
pip3 install pyyaml
python3 scripts/search.py "Python asyncio" --json
python3 scripts/mcp_server.py
```

### 方法四：git clone（開発 / ソース改変）

```bash
git clone https://github.com/taxueseek/argo.git
cd argo
pip3 install pyyaml
bash scripts/install.sh --link ~/.claude/skills/argo   # 任意
python3 scripts/search.py --list-engines
```

### 方法五：Skill ディレクトリへ（シンボリックリンク、単一来源）

```bash
python3 scripts/link_source.py --to ~/.claude/skills/argo
python3 scripts/link_source.py --to ~/.agents/skills/argo

cp installs.local.yaml.example installs.local.yaml
python3 scripts/link_source.py
python3 scripts/link_source.py --check
```

### 方法六：Python ライブラリとして

```python
import sys
sys.path.insert(0, "/path/to/argo/scripts")
from search import super_search

result = super_search("Python asyncio", n=5, mode="fast")
for item in result["results"]:
    print(item["title"], item.get("credibility_fast"), item["url"])
```

```bash
# bin/argo を PATH に入れた場合
argo search "Python asyncio"
argo research "2026 公募ファンド保有構造"
argo evidence "検証したい主張"
```

---

## 対応プラットフォーム

| プラットフォーム | 接続 | 説明 |
|------------------|------|------|
| **Claude Code** | MCP / Skill | `npx` または `mcp_server.py`；`link_source.py` 可 |
| **Kimi / Grok Build** | MCP Server | 同上 |
| **Cursor / Cline / Continue** | MCP | MCP 対応 IDE プラグイン |
| **CLI** | `search.py` / `bin/argo` | スクリプト、cron、手動調査 |
| **Python プロジェクト** | `from search import super_search` | ライブラリ呼び出し |

### インストール後チェック

```bash
python3 --version          # 3.9+ が必要
python3 -c "import yaml; print('PyYAML OK')"
python3 -m pytest tests/test_unit.py -q   # 任意
python3 scripts/search.py --list-engines
```

---

## できること

| 機能 | 説明 | 入口 |
|------|------|------|
| 統合検索 | ルート → 召回 → 融合 → 速評 | `search.py` / `argo_search` |
| ローカルファイル検索 | 本機のコード/ノート/記憶（非ネット） | `argo_local_search` |
| ローカルテキストプレビュー | ホワイトリスト内プレビュー（fail-closed） | `argo_local_read` |
| 再計算 | 制限付きサブプロセスで数値再計算（既定拒否） | `argo_recompute` |
| 深掘り調査 | サブ問題、多ソース、ギャップ提示 | `research.py` / `argo_research` |
| 信頼度評価 | 権威 / 密度 / 鮮度 / 交差検証 | `evidence.py` / `argo_evidence` |
| 意図の曖昧さ解消 | 多義語、ブランド衝突、戦略提案 | `clarify.py` / `argo_clarify` |
| ページ取得 | HTTP 優先、必要ならブラウザ | `argo_fetch`（`mode=extract` で構造化） |
| スクショ / PDF | 画面キャプチャ、PDF 構造抽出 | `argo_screenshot` / `argo_pdf` |
| サイトクロール | 一覧ページの一括取得 | `argo_crawl` |
| クエリ演算子 | `site:` / `-site:` / `-語` / `-"句"` / `"句"` / `lang:` / `after:` / `before:` をルーティングと検索の両側から剥離（詳細は references/usage.md） | クエリに直接記述 |
| 信頼シグナル | `authority_level` 離散等級（0-4、≥3 で極めて権威）+ `summary` Agent 档要約（検証後に逆填み） | 結果フィールド、`--fields agent` で表示 |
| SNS / 世論 | 微博 / 小红书 / B 站 / Reddit / X など | `argo_social_search` |
| DSH ネイティブツール | DSH プラグインとして入れると `argo_search` / `argo_fetch` などが宿主の一等ツールになり、CLI 単発で実行・常駐トークン開銷ゼロ | `dsh plugin add` 後に直接呼ぶ |
| DSH web seam | 宿主内蔵の `web_search` / `web_fetch` が argo のエンジンチェーンと取得チェーンへルーティング（HTTP 優先、Cloudflare / JS 描画では自動でブラウザへ昇格） | プラグインが既定で登録、profile で `searchProvider` / `fetchProvider` を pin |
| DSH 設定ページ | 接入 / ツール / 引擎 / 密钥 / 研究 / 高级 の 6 分区、保存即有効。キーは宿主の凭证库へ、平文は非表示 | 設定ポップアップ `Cmd+,` → 左メニュー「Argo」 |

### 予算モード

| モード | 向き | 振る舞い |
|--------|------|----------|
| `fast` | 簡単な問い、速度優先 | 無料エンジン優先、有料リランク省略 |
| `auto` | 日常デフォルト | コストと品質の折衷 |
| `deep` | 調査・総説 | 品質優先、エンジン増可 |
| `budget` | 枠が厳しい | クォータ制御、使い切ると劣化 |

### 現時点の能力概要（v2.9.9）

Argo のやることを一行で言うと、質問を領域と言語で適切なソースに振り分け、返ってきた結果を信頼度で並べ、エージェントには並べ替えられ検証できる素材を渡す。検索・取得・証拠確認・ローカルファイル検索は同じパイプラインを流れ、19 個の MCP ツールは CLI コマンドと一対一で対応するので、どの形态で入れても能力は欠けない。単独コマンドもいくつかある：argo tweet は X 投稿をまとめ、argo auth はログイン状态を管理し、argo job は採用情報を集約する。全ソースの費用・键・状态は docs/ENGINE_CATALOG.md に清单されている：284 ソース、241 無設定、99 ドメイン。

DSH プラグインとして入れると、これらの能力は宿主のネイティブツールになる。高頻度の検索と取得は既定でオン、長尾フォレンジックは設定ページで必要に応じてチェック、argo 本体の 28 個の挙動スイッチ（キャッシュ、再順位、重複除去、ガード、プロキシ、TTL など）は環境変数からパネルへ移った。キーは宿主の凭证库に入り、平文は表示されず、変更は保存即有効。

最近数版が、それぞれ一类の問題を直した。

v2.9.9 はセキュリティと提供品質の版です。コンテンツセキュリティエンジンは採点だけで止めていなかったため、高リスクの注入ページがそのまま文脈に入っていました。今は閾値を超えると隔離し、洗浄済みの本文で差し替え、証拠スコアにも入りません。ネットワーク出口には二つの防御を追加しました。リダイレクトはホップごとに検証し、DNS 解決の結果は当該接続に釘付けします。ディスパッチ側では、早期停止で打ち切られた遅いエンジンの成果をもう捨てません。バックグラウンドで回収し、猶予時間内に完成すれば通常どおり提供して学習器に実績を記録します。ルーティングは六族の実シナリオ走査で五類の誤ルーティングを収めました。A 株の財務クエリ、企業デューデリ/インボイス/人から会社を探す、民事実務、薬の説明書、論文執筆 howto がそれぞれ所定の場所へ。ほかに argo engine add で新エンジンを一段階で接続でき、DSH 設定ページにはルーティング観測カードとツール推奨コンボが加わりました。

v2.9.8 はルーティング精度とフィードバック閉環の版です。issue #18 の三つ——「latest」がリアルタイムニュースに誤判定される、people_daily が一致しないときに既定 feed を返す、local_bing が「2026」だけに一致する——をすべて再現して修正しました。同じ観点で実クエリを再生し、さらに 16 クラスの誤発火を絞り込みました。ブランド名の「雪」、製品名の「火山引擎」、UI トラブルシュートの「アイコン」、工商・金融フィールドの「コード」、コード文脈の「為替レート」などです。SERP カバレッジゲートと CJK クロス検証も入れ、「結果が返った＝関連がある」とは限らないようにしました。再現と特定に協力してくれた @wpp666 に感謝します。

v2.9.7 が直したのは三つの古い毛病。DSH 宿主内の AI が URL を読むとき、宿主自身の取集を使っていたため、短链ジャンプや JS チャレンジページは构造的に失败しがちで、argo の取得链は出番がなかった；この版で宿主の読込が argo を流れるようになり、そうしたページは失败から成功に変わり、普通のページは変わらない。路由候选池には 18 个の明示専用エンジンが混在し、自动选択されれば唯一の有料エンジンが密かに课金される风险があった；今は候选池になく、使うには明示が必要。エンジンが成功するたびに熔断器がファイル锁でディスクを书き换えていた；健全状态ではファイル开销がゼロになり、复数エンジンの记账も一件のトランザクションにまとまる。License は MIT から Apache-2.0 に変わり、商用での利用は安心感が増した。

その後 DSH プラグインにもう一層加わった。設定ページは五分区から六分区に増え、新しい「引擎」分区が argo 本体の挙動スイッチをパネルに収める（従来は環境変数のみ）。キー页では各キーが凭证库、起動環境、`.env` のどれ由来かが見えるようになり、ツールチェック・タイムアウト・並行数・研究上限はすべて保存即有効になった。キャンセルも接続確立段階まで到達し、AI が途中で止めると、起動中の argo サブプロセスはタイムアウトを待たずに即座に殺される。

v2.9.6 が担当したのはクエリ演算子と传输层。「site:github.com 某某」式のクエリは、演算子が普通の语として扱われたり、解析するだけで过滤されなかったりした；契约层ができて、路由とキャッシュは剥いたクエリだけを认识し、site: は真の后置フィルタとして実行されキャッシュキーにも入るので、二回の検索が混ざらない。传输层では、POST のたびに TLS ハンドシェイク（320〜561 ms）を再支払いしていた；接続プール化で、常驻の MCP プロセスは毎回の质问でそれを省ける。この版で公式 DeepSeek 検索も入り、従量课金で日常の combo には入らない。

v2.9.5 で加わったのは为替と重複排除の浄化。「人民币/美元 汇率」と寻ねると、宏观ドメインを回って百科词条に着地し、1.4 秒で答えなしだった；为替専用域ができて 675 msで即期レートと毎日のニュースを返す。重複排除に站后缀归一と fuzzy 复核が加わり、标题に区分式のある公式ドキュメントが后方に压されることがなくなった。

v2.9.4 が直したのは明示エンジン指定の経路。--engine X を指定するとキャッシュ TTL が一刀切だった；域判定が入って TTL は域に従い、予备エンジンがなければ同エンジンの空跑もしない。

版ごとの详细は「最近更新」に、各版のリリースノートは docs/ にある。
---

## エンジンとルーティング

設定上は **284** ソース（うち **241** はキー不要）、**99** ドメイン（`config.yaml` と `--list-engines` が基準）。

### 直結・垂直（抜粋）

| エンジン | 用途 | コスト傾向 |
|----------|------|------------|
| anysearch / duckduckgo | 一般 / 技術 | 無料 |
| sina_quote / tencent_quote / eastmoney | A 株 / 資金 | 無料 |
| finviz / seeking_alpha | 米株・海外金融 | 設定次第 |
| imdb / itunes / thesportsdb | 映像 / 音楽 / スポーツ | 主に無料 |
| local_openstreetmap / wikidata / wikipedia | 地理 / 組織 / 百科 | 無料 |
| arxiv / semantic_scholar / openalex | 学術 | 主に無料 |
| pubchem / gbif / rfc_editor | 化学 / 種 / 標準 | 無料 |
| github / stackoverflow / pypi / npm | コードとパッケージ | 設定次第 |
| byted / bocha / metaso / octen / yuanbao | 中国語ウェブ / AI 検索 | API / 低コスト |
| hkex / shfe / cfets / defillama / gleif | 香港公告 / 上期所 / 人民元中間価格 / DeFi / 法人識別子 | 無料 |
| zhihu / wechat_sogou | 中国語意見 / 公式アカウント | API / 無料 |
| tavily / felo / exa | 国際 / 意味検索 | 有料または枠 |
| twitter / reddit / xiaohongshu / bilibili / weibo | SNS UGC | 無料（一部ログイン） |

### ローカルゼロコスト層（`local_*`）

独立した SearXNG は不要。主経路はプロセス内 HTML / RSS / JSON 解析（`local_bing`、`local_sogou`、`local_google`、`local_arxiv` など）。**多言語クエリ**ではエンジンの言語パラメータ（例：Bing `setlang`）を動的に書き換え、RRF 融合に参加します。

---

## 使用例

### 金融

```bash
python3 scripts/search.py "python asyncio error handling" --explain
# 典型：stock_query → 相場スナップショット
```

### 学術

```bash
python3 scripts/search.py "transformer attention mechanism paper" --json
# domain は academic になりやすく、arxiv などを含む
```

### 研究と検証

```bash
python3 scripts/research.py "2026 公募ファンド第2四半期 保有構造" --depth deep --json

python3 scripts/search.py "同一クエリ" --json | \
  python3 scripts/evidence.py "同一クエリ" --stdin --json
```

### MCP ツール一覧（12）

| ツール | 用途 |
|--------|------|
| `argo_search` | 統合検索 |
| `argo_local_search` | ローカルファイル（非ネット） |
| `argo_local_read` | ホワイトリスト内テキストプレビュー（fail-closed） |
| `argo_recompute` | 制限付きサブプロセス再計算（既定拒否、要認可） |
| `argo_research` | 深掘り研究（social-sentiment モード含む） |
| `argo_evidence` | 信頼度評価 |
| `argo_clarify` | 意図の曖昧さ解消 |
| `argo_fetch` | スマート取得（`mode=extract` で構造化） |
| `argo_crawl` | サイトクロール |
| `argo_screenshot` | 画面キャプチャ |
| `argo_pdf` | PDF 抽出 |
| `argo_social_search` | 多プラットフォーム SNS（`mode=sentiment`） |

---

## インストールと設定

### 環境要件

| 項目 | 要件 |
|------|------|
| Python | 3.9+（CLI と MCP コア） |
| 依存 | `pip install pyyaml`（唯一のハード依存） |
| Node.js | **`npx` 入口のみ** 18+ |
| SearXNG | 不要（内蔵ローカルエンジン） |

### API Key（すべて任意）

未設定なら該当エンジンをスキップし、無料がバックストップ。**環境変数を使い**、本物の Key をリポジトリや Issue に書かないでください。

```bash
# 推奨（品質向上）
export TAVILY_API_KEY="your_key"
export BOCHA_API_KEY="your_key"
export METASO_API_KEY="your_key"
export ZHIHU_ACCESS_SECRET="your_key"

# 任意
export BRAVE_API_KEY="your_key"
export FELO_API_KEY="your_key"
export GITHUB_TOKEN="your_key"
export WEB_SEARCH_API_KEY="your_key"
export ANYSEARCH_API_KEY="your_key"
export OCTEN_API_KEY="your_key"
export ARGO_KEENABLE_API_KEY="your_key"   # 任意；Keenable 無料体験
```

`config.yaml` には `{ENV_NAME}` プレースホルダのみ。平文の秘密はコミットしません。

### キャッシュ

既定 SQLite は `config.yaml` の `cache.db_path`（通常 `~/.cache/unified-search/cache.db`）。

| 種別 | おおよその TTL |
|------|----------------|
| 金融 | 約 5 分 |
| ニュース / リアルタイム | 約 10–15 分 |
| 一般 | 約 1 時間 |
| 研究 / 常緑 | 約 2–24 時間 |
| 空結果 | ごく短い（失敗を固定しない） |

### FAQ

**API Key なしで使える？**  
使えます。ローカル無料エンジンと無料 API が多く、未設定時は自動で無料経路。

**インストールスクリプトと npx の違いは？**  
スクリプトは本機固定・設定変更・Skill 接続向き。npx は MCP を素早く載せる向き。コアは同じ Python。

**エンジン確認は？**  
`python3 scripts/search.py --list-engines`、または `--explain`。

**コードが複数コピーされる？**  
しません。`link_source.py` で単一来源 + シンボリックリンクを推奨。rsync 複製はしない。

---

## CLI よく使う引数

```
python3 scripts/search.py [options] クエリ

  --engine, -e       エンジン、既定 auto
  --max-results, -n  件数、既定 5
  --depth, -d        fast | balanced | deep
  --mode             fast | auto | deep | budget
  --no-cache         キャッシュを飛ばす
  --explain          ルーティング説明を表示
  --json             JSON 出力
  --timeout, -t      タイムアウト秒
  --list-engines     エンジン一覧
```

---

## 設計の取捨

1. **エージェントが吸収できることを先に、リンク数は後。**  
2. **無料とローカルを先に、有料は任意の上乗せ。**  
3. **失敗は観測可能に**：空結果 / タイムアウト / ブレーカを分け、黙って飲み込まない。  
4. **設定駆動でエンジン拡張**、`config.yaml` が単一来源。  
5. **単一来源インストール**：リンクし、rsync しない。  
6. **SNS を真理庫にしない**；拡張と世論向きで、単独の真値には不向き。

---

## 向いている用途

- Claude Code / Grok Build / Codex / Kimi など **エージェントの検索バックエンド**  
- **多言語・多領域**の日常 Q&A：中英日韓 + 金融 / 映像 / スポーツ / 学術 / コード  
- **再現可能・キャッシュ可能**なスクリプトとパイプライン  
- ファクトチェック、公開金融情報、エンティティの **多ソース対照**

単独で向きにくいもの：プラットフォーム内の高度なエンゲージメント順位付け、長期運用の最大召回アグリゲータ（外付け SearXNG 主経路は内蔵ローカルエンジンに置換済み）。

---

## ディレクトリ（簡易）

```
argo/
├── README.md                # 中国語（既定）
├── README.en.md             # English
├── README.ja.md             # 日本語
├── README.ko.md             # 한국어
├── README.es.md             # Español
├── SKILL.md
├── package.json             # npx 入口
├── bin/argo.js              # Node MCP 起動
├── bin/argo                 # Python CLI
├── config.yaml              # エンジンとドメイン（来源）
├── assets/readme/           # README ビジュアル
├── backends/
├── mcp/                     # 多クライアント MCP 注入の来源（clients.yaml）
├── scripts/                 # search / research / mcp / install …
├── sub-skills/local-search/
├── sub-skills/ego-search/      # ログイン態の専門検索（既定オフ）
├── tests/
└── docs/
```

---

## 最近の更新

直近 3 バージョンのみを載せています。完全な履歴は[更新履歴](#更新履歴)、各版の詳細は [Releases](https://github.com/taxueseek/argo/releases) を参照してください。

### v2.9.9：多層セキュリティ + 遅いソースの成果を捨てない + 誤ルーティング五類の収口

- **高リスク本文は文脈に入らない**：コンテンツセキュリティエンジンを「採点のみ」から「隔離＋洗浄差し替え」へ昇格。`risk_score ≥ 0.5` の本文は洗浄版で置き換えられ、証拠スコアには入りません。既定で有効、`ARGO_CONTENT_RISK_GATE=0` で緊急解除
- **ネットワーク出口の多層防御**：リダイレクトのホップごと検証（`ARGO_MAX_REDIRECTS`、既定 5）＋ 解決即釘付けで DNS リバインディング窓を封鎖
- **早期停止で打ち切られた遅いソースの成果をもう捨てない**：打ち切られたエンジンはバックグラウンド回収へ。`ARGO_TAIL_COMPLETE_S`（既定 3 秒、0=無効）内に完成すれば通常どおり提供し、実績を学習器に記録、成果は重複排除してキャッシュへ統合
- **誤ルーティング五類を収口**：A 株財務クエリは `stock_query` へ、企業デューデリ/インボイス/人から会社は `company_search` へ、民事実務は `flk_law` へ、薬の説明書は「用法」に奪われず、論文執筆 howto は学術検索に落ちない
- **`argo engine add`**：新エンジンを一段階で接続。spec/env/検証/同期/combo/許可リストの手作業チェーンを一コマンドに
- **シナリオ別バケット学習 + ZCode プラグインパッケージ**：ルーティングが「エンジンは A ドメインに強く B ドメインに弱い」を区別できるように。skill+CLI 直結とゼロトークンパネルを zcode-plugins へ提供

### v2.9.8：ルーティング精度監査 + Tencent Yuanbao 検索 + キー不要ソース 3 件 + 構造化された失敗リスト

- **issue #18/#19 を全修正**：「latest」の誤判定、people_daily の既定 feed、local_bing の「2026」のみ一致、中国語クロス検証が恒常 0、nightly の空ログクラッシュ。同型の誤発火 16 クラスもさらに絞り込み
- **Yuanbao 検索を追加**：中国語の一次情報・公式系ソース。中国語汎用ドメインの 2 番目に置き、権威ソースのヘッジとします。キーは `ARGO_YUANBAO_API_KEY`
- **キー不要で使えるソースを 3 件追加**：`tavily_free`、`keenable_free`、`exa` の匿名フォールバック
- **失敗の内容に応じて対処できる**：失敗はソースと種別ごとに「再試行 / ソース切替 / キー設定」と助言を示し、`meta.uncertainty` の信頼度も付きます
- **ローカル設定オーバーレイ `config.local.yaml`**：このマシン固有の調整が公開設定に書き込まれなくなります
- **DSH 設定ページに「エンジン」セクションを追加**：argo 本体の 28 個の挙動スイッチを環境変数からパネルへ移しました

### v2.9.7：アーキテクチャの収束 + ホストのページ取得を argo へ + License を Apache-2.0 に

- **License 変更**：MIT → Apache License 2.0。特許許諾を明示したもので、使い勝手は変わりません
- **ホストが取得できなかったページが取れるように**：短縮リンクの転送、プロトコルの引き上げ、リダイレクト用の中間ページ、Cloudflare / JS チャレンジのページを argo の取得チェーンが担当します（HTTP 優先、拒まれたらブラウザへ自動で昇格）。既定で有効、`fetchProviderEnabled` で切れます
- **auto モードが有料エンジンを誤って呼ばなくなる**：明示指定専用の 18 エンジンをルーティング候補プールから外し、`--engine` で指名したときだけ使います
- **新しいキーなら次のホップで復帰**：クォータを使い切ったエンジンが復帰を待たなくなり、ルーティングキャッシュも再計算されます
- **crawl / research / MCP から JS を実行しても落ちない**

---

## 更新履歴

| 版 | 内容 |
|----|------|
| **v2.9.9** | **多層セキュリティ + 遅いソースの成果を捨てない + 誤ルーティング五類の収口**：高リスク本文の隔離＋洗浄差し替え（証拠スコアから除外）、リダイレクトのホップごと検証と DNS リバインディング封鎖。早期停止で打ち切られた遅いソースはバックグラウンド回収（`ARGO_TAIL_COMPLETE_S`）で実績を学習器に記録。六族の実シナリオ走査で学術/財務/企業/法律/医薬の誤ルーティングを収口。`argo engine add` の一段階接続、シナリオ別バケット学習、ZCode プラグインパッケージ。詳細は [リリースノート](docs/RELEASE_NOTES_v2.9.9.md)。 |
| **v2.9.8** | **ルーティング精度監査 + issue #18/#19 修正 + テスト状態の隔離**：実クエリ再生で 16 クラスの誤発火（ブランド名・製品名・フィールド名・ASCII 部分文字列）を絞り込み、SERP カバレッジゲートと CJK クロス検証を追加。nightly の空ログ、people_daily の既定 feed、キャッシュ命中ガードを修正し、adaptive/breaker 状態をテストごとに隔離。再現と特定に協力してくれた @wpp666 に感謝。詳細は [リリースノート](docs/RELEASE_NOTES_v2.9.8.md)。 |
| **v2.9.7** | **アーキテクチャ収束 + DSH web_fetch 連携 + License を Apache-2.0 へ**：宿主の web_fetch を argo の取得チェーンに接続し、JS チャレンジページとクロスオリジンのリダイレクトが失敗から成功に；明示指定専用の 18 エンジンをルーティング候補プールから外し、auto モードの有料エンジン誤課金を遮断。 |
| **v2.9.6** | **DeepSeek Search 対応 + クエリ演算子契約 + 伝送層の二重課金解消**：新エンジン `deepseek_search` が既定で有効（445-510ms、権威階層・rerank_score・公開時刻を唯一すべてカバー）；クエリ演算子を 1 つの契約層に集約し、`site:` が実際の絞り込みとして動作。 |
| **v2.9.5** | **為替専用ドメイン + 重複排除の二層化 + 順位判定の誤り修正**：新ドメイン `fx_query` がレートと仲値を直接返す（従来は百科事典の項目に流れていた）；重複排除にサイト接尾辞の正規化とあいまい一致を追加。 |
| **v2.9.4** | **明示エンジンのドメイン帰属 + 回復チェーンの空振り停止 + ゲート同期**：`--engine X` にドメイン判定を補い、キャッシュ TTL の一般化を修正；代替のない単一エンジンでは回復チェーンを空振りさせない。 |
| **v2.9.3** | **X 投稿の完全パッケージ + ブラウザログイン態のレーン + ローカルチェーンの質と速度**：`argo tweet`（本文・スレッド・引用を一括梱包）と `argo auth`（Cookie は Chrome の暗号化ストレージのみ）を追加；ローカルチェーンがリダイレクト用中間ページをセッションなしで解決。 |
| **v2.9.2** | **DSH 0.2.1-alpha.1 適合検証 + ルーティング/ガードの修正一括 + js-run サブスキル**：DSH の三形態が最新版で動くことを実機検証し、`argo mcp inject` が 7 番目のホストまで対応；ブラウザなしでウェブ JS を走らせる js-run サブスキルを追加。 |
| **v2.9.1** | **コンテンツ品質の最適化 + コンテンツファーム対策 + 最新版への適合**：低品質な大量生成コンテンツの識別と除外を強化；ローカル検索に自身のインストールツリーのガードを追加し、結果が自身のソースで汚染されないように。 |
| **v2.9.0** | **ソース最適化 + プラグインと CLI の対等化 + 少数言語の拡充**：ソース 237→253（キー不要 198→219）で少数言語ニュース源 14 件と CORE 全文源を追加し、百度と捜狗を復活；インポートチェーン −79%、再ランキング 33ms→1.5ms；`argo stats` のローカル使用ログを追加。詳細は [リリースノート](docs/RELEASE_NOTES_v2.9.0.md) |
| **v2.8.9** | **出力のスリム化 + 検索の高速化と網羅性 + 海外メディアとファクトチェック源 5 件**：既定出力 15.5KB→5.2KB（−66%）で同じ結果を三度書かなくなり；リコール +35%、本文構造の再現 0/6→6/6；キー不要の新源 The Guardian / France 24 / DW / FactCheck / Full Fact を追加。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.9.md) |
| **v2.8.8** | **実ユーザー報告の修正（#12 キー別名の無言失敗、#13 プロキシ非対応の取得）+ 全面高速化 + ソース 218 → 235**：出口ディスパッチを統一して `ARGO_PROXY` / ドメイン別ルール / `NO_PROXY` を尊重；コマンド冷起動 2.1s→0.55s；`argo answer` の直答と `argo watch` の Web 観測、キー不要源 14 件を追加。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.8.md) |
| **v2.8.7** | **218 ソース / 89 ドメイン + 本文直出 3 チャネル + ルーティング発火の規律**：バッチ 7/8/9 で累計 50 の新ソースを追加；取得チェーン第 0 段に llms.txt と `.md` の直出プローブ；中国マクロ照会では国家統計局を前置。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.7.md) |
| **v2.8.6** | **hedged 競速 + 知乎の三源分担 + 宣言的な言語ディスパッチ + 学術検索プロトコル**：先頭エンジンに猶予窓を設けた競速で、速いエンジンは呼び出し 1 回で済み；知乎の全体検索を接続し、サブクエリの言語・学術ディスパッチを追加。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.6.md) |
| **v2.8.5** | **DSH プラグインツールのネイティブ化 + MCP 既定オフ + Windows 互換 + クォータ自己修復**：`argo_search` / `argo_fetch` がネイティブ一等ツールとして既定で使えるように；Windows 全対応（一時パス・GBK・`install.ps1`）；クォータ枯渇を自動検出して代替源に切り替え、期間終了で自己修復。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.5.md) |
| **v2.8.4** | **ローカルデータ融合 + マルチクライアント MCP 注入 + 構造化検索の強化**：深い調査が手元の一次データを取り込めるように（`file_inputs` + `recompute`）；`argo mcp inject` の 1 コマンドで 6 クライアントに注入；クエリ正規化と複雑さゲートを追加。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.4.md) |
| **v2.8.3** | **多言語ルーティング修正 + プロセス内 anysearch + weighted RRF**：ja/ko は対象言語を返し、独仏西伊は anysearch 経由；weakest-link で弱い源を降権。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.3.md) |
| **v2.8.2** | **Windows 全対応 + 証拠セマンティクスの統一**：npm の `os` 制限を撤去し、GBK クラッシュを UTF-8 で根本対処；メインパッケージに `dsh.bundle` を追加；`wide_research` に品質ゲート。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.2.md) |
| **v2.8.0** | **証拠の一貫した流れ + 求人検索 v3 + 天気の二源化**：`fetch_required` と `--verify` で検証済みリンクを記憶；`argo job` の求人検索 v3；wttr.in + Open-Meteo の二源。詳細は [リリースノート](docs/RELEASE_NOTES_v2.8.0.md) |
| **v2.7.3** | **今回の修正 + エンジン活性化**：エンジン層に HTTP クライアント（UA ローテーション / 再試行 / リダイレクト追従）を導入し、arxiv が 5s タイムアウトから 2s 以内に 10 件；TF-IDF の意味注入で 25 の垂直エンジンを活性化；70 ドメインの TTL を整備。詳細は [リリースノート](docs/RELEASE_NOTES_v2.7.3.md) |
| **v2.7.2** | **ログイン態の専門検索**：ego-search サブスキルを追加（既定オフ、有効化方法は上部の節を参照）；日韓のクエリに中国語エンジンが混ざらないように。詳細は [リリースノート](docs/RELEASE_NOTES_v2.7.2.md) |
| **v2.7.1** | **セキュリティ強化 + ルーティング修正**：SSRF 対策（URL 許可リスト + IP 帯チェック）；ルーティング健全性の意味のずれを根本修正。詳細は [リリースノート](docs/RELEASE_NOTES_v2.7.1.md) |
| **v2.7.0** | **垂直構造化モーダルカード**：内蔵の `bocha` / `bocha_ai` ネイティブエンジン、`modal_card` ドメインが列車チケット・ガソリン価格・貴金属・万年カレンダー・星座などのリアルタイムカードを統一認識。詳細は [リリースノート](docs/RELEASE_NOTES_v2.7.0.md) |
| **v2.6.2** | 独立した改良ラインを統合：ネットワーク環境認識 / 加重 RRF と意味キャッシュ / 適応型エンジン無効化 / コンテンツ安全とクエリ変体 / 三大垂直エンジン；v2.6.1 のルーティング修正を含む。詳細は [リリースノート](docs/RELEASE_NOTES_v2.6.2.md) |
| **v2.6.1** | v2.6.0 修正版：ルーティングの巻き込み修正（`capital of` が fact_check を奪わない）；バージョン同期。詳細は [リリースノート](docs/RELEASE_NOTES_v2.6.1.md) |
| **v2.6.0** | **多言語検索**（検出 / エンジンパラメータ / 言語横断フォールバック）；映像・スポーツ・地理・組織・メディアの垂直を補完；約 120+ 源。詳細は [リリースノート](docs/RELEASE_NOTES_v2.6.0.md) |
| **v2.5.1** | 金融/マクロ/化学の答え源を厚く；エンジン階層 + combo 予算；[v2.5.1](docs/RELEASE_NOTES_v2.5.1.md) |
| **v2.5.0** | インストールスクリプト + npx；書き換えとルーティング分離；ホットパスキャッシュ；MCP コンパクト |
| **v2.4.0** | 低分ルートのフォールバックと SNS 誤吸フィルタ；キャッシュ depth / ソフトヒット；ブレーカと負キャッシュ；`engine_outcomes` |
| **v2.2–v2.3** | 証拠二段階、中国語ソース表、content_signals、fetch スタック、エンジン拡充 |
| **v2.1** | SNS エンジン層（多プラットフォーム UGC） |
| **v1.x** | 名称を Argo に統一、多エンジンルーティングと二層キャッシュ |

---

## 貢献

Issue と Pull Request を歓迎します。ルーティングや証拠ロジックを変えるときはテストを足してください：

```bash
python3 -m pytest tests/test_unit.py tests/test_multilingual.py -q
python3 scripts/regression_p0p1.py --offline
python3 scripts/matrix_search_eval.py --offline
python3 scripts/ab_eval_p0p1.py   # 任意、オンライン含む
```

コミット前：本物の API Key、本機絶対パス、cookie などを含めないこと。本機 Skill パスは `installs.local.yaml`（gitignore 済み）。

## License

Apache License 2.0 © 2026 [taxueseek](https://github.com/taxueseek)

<p align="center">
  <a href="https://github.com/oil-oil/beautify-github-readme"><img src="assets/readme/made-with-beautify.svg" width="300" alt="README made with beautify-github-readme"></a>
</p>

---

> 良い検索は、もっと多く見せることではない。自信を持って結論できること——そしてまだ結論してはいけないときを知ることだ。
