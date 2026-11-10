# twincat-ads-mcp スケルトン

GitHub Copilot から TwinCAT ADS 経由で PLC 実機の変数を **読み取り専用** で
観測し、デバッグに使うための MCP サーバ一式。
監視は **ADS Device Notification 前提** (ポーリングしない)。
詳細仕様は **SPEC.md** を参照。

## 構成

```
(このフォルダを TwinCAT ソースプロジェクトのルートに置く)
├── SPEC.md                           仕様書
├── .vscode/mcp.json                  Copilot (VS Code) の MCP 設定 (実機用 + モック用)
├── .github/copilot-instructions.md   デバッグ手順の指示書
└── tools/ads-mcp/
    ├── server.py        MCP サーバ本体 (読み取り専用)
    ├── mock_plc.py      模擬 PLC (実機なしで全ツールを試す用)
    ├── config.yaml      接続設定 (AMS Net ID / ADS ポート / ルータ記録)
    ├── groups.yaml      デバッグループ定義
    ├── smoke_test.py    MCP 化前の疎通確認スクリプト
    ├── windows/Setup-AdsRouter-Check.ps1   AI 側ルータ導入チェック
    ├── tests/           pytest 一式 + モック E2E スクリプト
    └── pyproject.toml   依存 (mcp 1.x / pyads / pyyaml、uv で解決)
```

前提: AI 側 Windows に Python 3.11+ と [uv](https://docs.astral.sh/uv/)、
そして **ADS ルータ (TC1000 = TwinCAT 3 ADS Setup)** が入っていること。
pyads は Windows では TcAdsDll + 自機ルータ経由で通信し、
ルートは TwinCAT Router UI で張る (SPEC.md §4)。

## 手順

### 0. 実機なしで試す (モック)
実機もルータも無しで、MCP + Copilot の流れだけ先に確認できる。
`uv run python tests/e2e_mock_mcp.py` で E2E を通すか、
VS Code で MCP サーバ `twincat-ads-mock` (登録済み) を起動して
Copilot に「example_axis を監視して」と頼む。
模擬 PLC は軸モデルが動き、位置・動作中フラグの変化イベントが返る。
異常シナリオを見たい時は環境変数 `MOCK_ERROR_SCENARIO=1`。

### 0.5. ルータ導入チェック (AI 側 Windows)
`windows/Setup-AdsRouter-Check.ps1` を実行すると、TcAdsDll・関連
サービス・TCP 48898 の状態を確認でき、TC1000 未導入なら手順が出る。

### 1. 疎通確認 (先に潰す)

`tools/ads-mcp/config.yaml` の `net_id` を対象 PLC に合わせ、
既知の変数名で:

```
cd tools/ads-mcp
uv run python smoke_test.py MAIN.<既知の変数名>
```

デバイス情報・現在値・Notification の受信まで確認できる。
繋がらない場合は AMS Net ID / ポート違い、PLC 側のルート未登録、
ファイアウォール (TCP 48898) のどれかであることが多い。

### 2. グループ定義

`tools/ads-mcp/groups.yaml` のサンプルを実在の変数名に置き換える。
「一緒に見るべき変数 + 正常条件 (normal)」が 1 グループ。
アナログ値は `mode: cycle` + `deadband`、チャタる入力は
`min_interval_ms` を付けるとイベントが溢れない。

### 3. Copilot から使う

VS Code でプロジェクトを開き、Copilot Chat を Agent モードにして
MCP サーバ `twincat-ads` を起動する (`mcp.json` は配置済み)。
あとは普通に頼むだけ:

> 軸1が途中で止まる。example_axis グループを監視して、再現させるから
> イベントを見て原因を考えて

Copilot は `start_watch` → (再現) → `get_watch_events` → ソース照合、
という流れで動く (手順は copilot-instructions.md に固定済み)。

## MCP ツール一覧 (全 10、書き込み系は無し)

| ツール | 役割 |
|---|---|
| `plc_info` | デバイス情報 + ADS 状態。接続確認 |
| `list_groups` | グループ一覧 |
| `get_group_snapshot` | グループ全値の一括読み (Sum Read) |
| `read_symbol` / `get_symbol_info` | 単発読み / 型・コメント取得 |
| `search_symbols` | シンボル名の部分一致検索 |
| `start_watch` | Notification 監視開始。baseline を返す |
| `get_watch_events` | 変化イベント取得 (seq で差分取得可) |
| `get_watch_status` | 現在値・イベント数・抑制数 |
| `stop_watch` | 監視終了・Notification 解除 |

## Notification 前提の設計メモ

- 変化検出は PLC 側。変化した変数だけが通知されるので、
  ポーリングより PLC / LAN の負荷が小さい。
- ただし「毎サイクル変わるアナログ値」は on_change だと通知が洪水になる。
  → groups.yaml で `cycle` (周期送信) か `deadband` (不感帯) を必ず付ける。
  deadband は「最後に記録した値」基準なので、緩やかなドリフトも
  蓄積すれば検出できる。
- コールバック内では ADS を呼ばない (デッドロック回避)。
  parse してメモリ上のリングバッファ (最大 5,000 件) に積むだけ。
- `suppressed` は間引かれた件数。Copilot には「間引かれている」ことが
  伝わるので、生データが必要な場面では groups.yaml の調整を提案させる。
- 配列・構造体・WSTRING は Notification 非対応のため snapshot_only に
  回り、開始時の baseline のみ対象になる。

## 読み取り専用の担保

1. サーバに write 系ツールを実装していない (pyads の write_* は未使用)。
2. 公開ツールはシンボル名ベースのみ。IndexGroup 直指定の汎用読みも無い。
3. 運用側では、対象 PC からの TCP 48898 のみ許可するのが望ましい。

注意: ADS プロトコル自体に「読み取り専用」の権限概念はない。
上記は「AI 経由では書けない」担保であり、他のツールからの書き込みは別問題。

## テスト

```
cd tools/ads-mcp
uv run --with pytest python -m pytest tests/ -q   # 7 件 (実機なし)
uv run python tests/e2e_mock_mcp.py               # MCP 経由の E2E (モック)
```

## 既知の制約 / 次の一手

- 監視セッションはサーバプロセス内メモリのみ。VS Code を閉じると消える。
  長期記録が要るようになったら SQLite へイベントを永続化する拡張が候補。
- MCP SDK は 1.x にピン留めしている (2.x で FastMCP が改名されたため)。
- 複数 PLC を見る場合は config を分けて MCP サーバを複数登録する。
- 実機疎通は未実施。受入手順は SPEC.md §11.2。

## 変更メモ

- 0.4: SPEC §12.3 の対策を実装。ADSError を恒常/接続系に分類し、
  恒常エラーでは接続リセットしない、リトライは読み取り系のみ
  (start_watch は対象外 + 部分登録の後始末)、接続リセット時は
  既存 watch を error 状態にして AI から見えるようにした。
  回帰テスト 3 件を追加 (計 7 件)。
- 0.3: SPEC に「実装で判明した知見と注意点」(§12) を新設。
  NotificationAttrib の単位や初期通知の扱い等の実測知見に加え、
  コード再読で見つけた接続リセット / リトライの副作用 (§12.3) を
  要改善として明記。実装自体の変更はなし。
- 0.2: AI 側 Windows に ADS ルータ (TC1000) を入れる構成を正式化。
  モックバックエンド追加で実機なし検証を一通り実施。
  NotificationAttrib の周期換算ミス (ms を渡すべき所に 100ns 換算済みの
  値を渡していた) を修正し、回帰テストを追加。
