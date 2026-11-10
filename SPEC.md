# 仕様書: TwinCAT ADS 読み取り専用 MCP サーバ

| 項目 | 内容 |
|---|---|
| 文書版 | 0.4 (2026-11-11) |
| 対象 | GitHub Copilot から TwinCAT PLC 実機の変数を読み取り専用で観測し、デバッグに使う環境 |
| 実装 | `tools/ads-mcp/server.py` (Python + pyads + FastMCP) |
| 状態 | スケルトン完成。モックでの全フロー検証済み。実機疎通は未実施 (受入手順は §11) |
| 0.3 の変更 | 実装を通じて判明した知見・注意点を §12 として新設し、関連節 (§8 / §10 / §13) に反映した |
| 0.4 の変更 | §12.3 の対策 (エラー分類・リトライ限定・watch の error 遷移) を実装し、回帰テスト 3 件を追加した |

## 1. 目的とスコープ

### 1.1 目的
- PLC ソースコードのプロジェクトから Copilot (Agent モード) を呼ぶ作業の中で、AI が実機変数の「現在値」と「変化の時系列」を直接観測できるようにする。
- 監視は ADS Device Notification を前提とし、ポーリングによる PLC / ネットワーク負荷を避ける。

### 1.2 対象外
- 変数値の書き換え、PLC の Start/Stop、ダウンロード等の制御操作は行わない (実装しない)。
- 長期の時系列記録 (DB 永続化) は本版の対象外。監視イベントはサーバプロセス内メモリのみ。
- github.com 上のクラウド Agent からの利用は対象外 (現場 LAN に到達できないため)。利用は AI 側 Windows 上のローカル Agent に限る。

## 2. 用語

| 用語 | 意味 |
|---|---|
| AI 側 Windows | Copilot と MCP サーバが動く Windows PC。本仕様ではここに ADS ルータを入れる |
| AMS Net ID | ADS のアドレス (例: 192.168.1.101.1.1)。IP + 2 オクテット |
| ADS ポート | 対象サービスの番号。TwinCAT 3 の PLC ランタイム 1 本目は 851 |
| デバッグループ | 一緒に観測すべき変数のまとまり。`groups.yaml` で定義する |
| watch | グループに対する Notification 監視セッション |
| baseline | watch 開始時点に一括読みした全変数の値 |

## 3. システム構成

```
[TwinCAT ソースプロジェクト]  (AI 側 Windows)
  VS Code + GitHub Copilot (Agent モード)
        |  .vscode/mcp.json (stdio 起動)
        v
  MCP サーバ (server.py / Python + pyads + FastMCP)
        |  TcAdsDll.dll
        v
  ADS ルータ (TC1000 で導入)  ← ルート表を持つ
        |  TCP 48898 (AMS/ADS)
        v
[PLC 実機] TwinCAT ランタイム (ADS ポート 851)
        (PLC 側にも AI 側への戻りルートが必要)
```

| コンポーネント | 採用 | 備考 |
|---|---|---|
| MCP サーバ | Python 3.11+ / FastMCP (mcp SDK 1.x) | 2.x は FastMCP が改名されたため 1.x にピン留め |
| ADS クライアント | pyads 3.x | Windows では TcAdsDll 経由 |
| AI 側ルータ | **TC1000 (TwinCAT 3 ADS Setup)** | §4 参照 |
| 設定 | YAML (config.yaml / groups.yaml) | §6 |
| 補助 | .github/copilot-instructions.md | Copilot のデバッグ手順を固定 |

## 4. AI 側 Windows の ADS ルータ仕様

### 4.1 導入物
- Beckhoff 公式 **TC1000 (TwinCAT 3 ADS)** の Setup を AI 側 Windows に入れる。TcAdsDll、AMS ルータ、Router UI (ルート編集) が入る通信専用パッケージで、XAE (開発環境) や XAR (ランタイム) より軽い。
- 入手経路: Beckhoff Download finder → Software and tools → TwinCAT 3 → TC1xxx → Runtime → TC1000 → TC3.1 ADS。ダウンロードには Beckhoff アカウント登録が必要。
- 導入確認は `tools/ads-mcp/windows/Setup-AdsRouter-Check.ps1` で行う (TcAdsDll の有無、関連サービス、TCP 48898 の Listen、Python / uv の有無をチェックし、未導入なら手順を表示する。自動インストールはしない)。

### 4.2 ルータの役割と前提
- pyads (Windows) は自機ルータ経由で通信する。ルート設定は pyads からではなく **TwinCAT Router UI** で行うのが Windows の作法。
- ルートは双方向に必要:
  1. AI 側ルータ → PLC: トレイの TwinCAT アイコン → Router → Edit Routes → Add (Name / PLC の AMS Net ID / PLC の IP / Transport=TCP/IP)。
  2. PLC 側 → AI 側 (戻り): XAE の SYSTEM → Routes で静的ルートを追加するか、`pyads.add_route_to_plc` を使う (PLC の資格情報が必要。unidirectional はオフ)。
- ファイアウォール: AI 側 → PLC 方向の TCP 48898 を許可する。ルート探索 (ブロードキャスト) を使う場合は UDP 48899 も関係するが、本構成では IP / Net ID を明示するため必須ではない。
- AI 側自身の AMS Net ID は Router UI / About TwinCAT System で確認し、`config.yaml` の `router.local_net_id` に記録する (動作には不要、運用記録用)。

### 4.3 採用しない代替
- **AdsRouterConsole / TwinCAT.Ads.TcpRouter (NuGet)**: TwinCAT 未導入環境向けの軽量 .NET ルータだが、対象は .NET の AdsClient であり、pyads + TcAdsDll の前提 (TwinCAT ルータ) を満たさない。MCP サーバを .NET で作り直す場合の選択肢としてのみ残す。機能制限 (ADS Secure 非対応等) もあるため本構成では採用しない。
- **ルータなし直接接続 (pyads の set_local_address 等)**: Windows では `set_local_address` 自体が使えない。Linux 構成専用の裏道であり、本構成では使わない (コード側でも Windows では呼ばないようガード済み)。

## 5. MCP サーバ仕様

### 5.1 起動
- トランスポート: stdio。`.vscode/mcp.json` の `servers.twincat-ads` が `uv run --project tools/ads-mcp python tools/ads-mcp/server.py` で起動する。
- 実機なし練習用に `servers.twincat-ads-mock` も登録済み (環境変数 `ADS_MCP_BACKEND=mock`)。
- ログは stderr (stdio の stdout は MCP 通信専用のため)。

### 5.2 バックエンド
| backend | 内容 |
|---|---|
| `pyads` (既定) | 実機。TcAdsDll + 自機ルータ経由 |
| `mock` | 模擬 PLC (`mock_plc.py`)。軸モデルが動き、Notification も本物と同じ形式で届く。実機なしで全ツールと Copilot の流れを確認する用。ADS プロトコル自体の検証にはならない |

切替は `config.yaml` の `backend` または環境変数 `ADS_MCP_BACKEND`。

## 6. 設定ファイル仕様

### 6.1 config.yaml
| キー | 意味 | 既定 |
|---|---|---|
| `backend` | `pyads` / `mock` | `pyads` |
| `net_id` | 対象 PLC の AMS Net ID | `127.0.0.1.1.1` |
| `port` | ADS ポート | `851` |
| `router.install` | 導入ルータの記録 | `TC1000 (TwinCAT 3 ADS Setup)` |
| `router.local_net_id` | AI 側の AMS Net ID (記録用) | null |
| `router.route_name` | Router UI のルート名 (記録用) | null |
| `local_net_id` | pyads 側で自機 Net ID を指定する場合 (Linux 専用) | null |

環境変数 `ADS_MCP_NET_ID / ADS_MCP_PORT / ADS_MCP_BACKEND / ADS_MCP_LOCAL_NET_ID / ADS_MCP_GROUPS` が優先される。

### 6.2 groups.yaml
```yaml
groups:
  <グループ名>:
    description: "AI への説明 (何のまとまりか)"
    normal: "正常条件を文章で (異常判断の基準)"
    mode: on_change        # グループ既定 (任意)
    symbols:
      - name: "MAIN.stAxis1.bError"          # 必須
        mode: on_change | cycle              # 任意 (既定 on_change)
        cycle_ms: 200                        # mode=cycle の周期
        deadband: 0.01                       # 数値の不感帯 (記録値基準)
        min_interval_ms: 50                  # 連続変化の抑制間隔
```
- `groups.yaml` はツール呼び出しのたびに読み直すため、編集にサーバ再起動は不要。
- 配列・構造体・WSTRING は Notification 非対応のため snapshot_only 扱い (baseline のみ)。

## 7. ツール仕様 (全 10、すべて読み取り)

| ツール | 入力 | 出力 (要点) |
|---|---|---|
| `plc_info` | なし | backend、net_id、port、router 記録、デバイス名、版、ADS 状態 |
| `list_groups` | なし | グループ名・説明・変数数 |
| `get_group_snapshot` | group | 全変数の現在値 (Sum Read) + 読取時刻 |
| `read_symbol` | name | 単一変数の現在値 |
| `get_symbol_info` | name | 型、PLC コメント、IndexGroup/Offset |
| `search_symbols` | pattern | 名前部分一致のシンボル一覧 (上限 200 件) |
| `start_watch` | group | watch_id、baseline、監視対象 / snapshot_only / エラー |
| `get_watch_events` | watch_id, after_seq, limit | 変化イベント列、抑制数、現在値 |
| `get_watch_status` | watch_id | 現在値、イベント数、抑制数、開始時刻 |
| `stop_watch` | watch_id | 解除結果、総イベント数 |

上限: 1 watch あたり 100 変数、同時 watch は 4、イベント保持は 1 watch あたり 5,000 件 (リングバッファ)。

## 8. Notification 設計

- 変化検出は PLC 側で行われ、変化した変数だけが通知される (ポーリング廃止が本仕様の前提)。
- モード:
  - `on_change` (ADSTRANS_SERVERONCHA): 変化時のみ。BOOL や状態量はこちら。
  - `cycle` (ADSTRANS_SERVERCYCLE): 周期送信。毎サイクル変わるアナログ値は、cycle + deadband の併用を標準とする。
- **周期換算の注意**: pyads の `NotificationAttrib` は max_delay / cycle_time を「ミリ秒」で受け取り、内部で ×10000 して ADS の 100ns 単位に変換する。呼び出し側で先に ×10000 すると周期が 10000 倍に化ける (本スケルトンで実際に踏み、回帰テストで固定済み。実測の詳細は §12.1)。
- deadband は「最後に記録した値」基準。緩やかなドリフトも蓄積すれば検出される。
- min_interval は受信側 (サーバ) の抑制。抑制された件数は `suppressed` として返し、AI が「間引かれている」ことを認識できるようにする。
- on_change で前回記録値と同値の通知はイベント化しない (登録直後の初期通知等の吸収)。
- コールバック内では ADS を呼ばない (ADS DLL スレッドからの再入でデッドロックし得るため)。parse とリングバッファへの追加のみ。ADS 呼び出しは単一接続 + ロックで直列化する。

イベント形式:
```json
{"seq": 12, "time": "2026-11-11T08:17:43.102+09:00",
 "symbol": "MAIN.stAxis1.bMoving", "value": true}
```
`time` は PLC 側タイムスタンプ (Notification のタイムスタンプ) を優先する。

## 9. 読み取り専用と安全

1. サーバに write 系ツールを実装しない。pyads の `write_*` はサーバから呼ばない (テストで書き込みが呼ばれたら失敗するガード付き)。
2. 公開はシンボル名ベースのみ。IndexGroup 直指定の汎用アクセスは公開しない。
3. ネットワーク側は AI 側 Windows からの TCP 48898 のみ許可するのが望ましい。

限界の明記: ADS プロトコル自体に「読み取り専用」の権限概念はない。本仕様の担保は「AI (Copilot) 経由では書けない」ことであり、ルートを持つ他のツールからの書き込みは別途ネットワーク側で管理する。また ADS ルートは双方向の信頼であるため、PLC 側ルートの追加は現場の管理手順に従うこと。

## 10. エラー処理

エラー処理は §12.3 の対策を実装済みで、`ADSError.err_code` で分岐する。

- **恒常エラー** (1808 シンボル不存在、1794/1795 インデックス不正、
  1796 権限なし、1827 アクセス拒否など `PERMANENT_ADS_ERRORS` の集合):
  接続は健全とみなし、リセットもリトライもせず即座に送出する。
  他の watch を巻き込まない。
- **接続系エラー** (上記以外。タイムアウト、対象未達、ポート未開など。
  未知のコードもこちらに倒す): 既存 watch をすべて `error` 状態にしてから
  接続をリセットする。読み取り系ツールは 1 回だけ再試行し、
  副作用のある `start_watch` は再試行しない (呼び出し側がやり直す)。
- `start_watch` が失敗した場合、部分登録した Notification があれば
  後始末を試みてから送出する (個別変数の登録失敗自体は従来どおり
  snapshot_only に吸収されるため、後始末は安全網としての位置づけ)。
- watch 登録に失敗した変数はセッション全体を止めず、snapshot_only + `errors` に理由を残す。
- 不明なグループ / watch_id は、既知の一覧を含むエラー JSON を返す (例外で落とさない)。
- `stop_watch` は Notification 解除を試み、失敗してもセッションを破棄する。
- `error` 状態の watch はイベント履歴を保持し、`get_watch_events` /
  `get_watch_status` が `state` / `state_detail` / `warning` を返す。
  復旧は `stop_watch` → `start_watch` の張り直しによる。

## 11. テストと受入

### 11.1 実機なし (実施済み)
- 単体・ロジック: `tools/ads-mcp/tests/test_server_logic.py` (pytest、7 件)
  - NotificationAttrib の周期換算 (回帰)
  - FakeConnection での watch → deadband 抑制 → stop の解除確認
  - 書き込みが呼ばれないことのガード
  - モックでの watch 実フロー (位置イベント ≥3 件、bError は無変化時にイベント化しない)
  - §12.3 対策 (回帰): 恒常エラーでリセットしない / 接続系エラーで
    読み取り回復 + watch が error 遷移 / start_watch はリトライされない
- E2E: `tools/ads-mcp/tests/e2e_mock_mcp.py` — 実 MCP クライアントから stdio で接続し、`plc_info → start_watch → get_watch_events (29 件) → stop_watch` を通過 (2026-11-11 実施)。
- 参考: pyads 公式テストサーバ (AdsTestServer) は複数 Notification のハンドルに既知の不具合 (pyads issue #471) があるため、本プロジェクトのモックは自前の `mock_plc.py` を正とする。

### 11.2 実機受入手順 (未実施 — 実機接続時に行う)
1. `windows/Setup-AdsRouter-Check.ps1` でルータ導入を確認する。
2. Router UI で PLC へのルートを張り、PLC 側の戻りルートを確認する。
3. `config.yaml` の `net_id` を対象に合わせる。
4. `uv run python smoke_test.py <既知の変数名>` で、デバイス情報・現在値・Notification 受信 (実機側で値を変える) まで確認する。
5. `groups.yaml` のサンプルを実変数名に置き換える。
6. VS Code + Copilot Agent モードで `twincat-ads` を起動し、`plc_info` → `start_watch` → 再現 → `get_watch_events` の流れを通す。
7. 受け入れ基準: (a) baseline が実値と一致、(b) 再現操作に対するイベントが時系列で取れる、(c) 監視中の PLC タスク負荷に実害がない、(d) `stop_watch` 後に通知が残らない。

## 12. 実装で判明した知見と注意点

本章は机上の設計ではなく、スケルトンを実際に実装・検証する過程で踏んだもの、
および完成コードの再読で見つけたものを記録する。再実装・改修時は先にここを読むこと。

### 12.1 ライブラリ仕様の落とし穴 (実測)

- **NotificationAttrib の単位は「渡す時」と「保持される時」で違う。**
  引数 max_delay / cycle_time はミリ秒で渡し、pyads が内部で ×10000 して
  100ns 単位に変換する。ADS の生仕様 (100ns) を知っていると先に ×10000
  したくなるが、それをやると値が化ける。実測では cycle 200ms のつもりが
  保持値 2820130816 (= 200 秒相当のオーバーフロー) になった。
  対策は「Attrib 生成は 1 箇所に集約し、保持値 == cycle_ms × 10000 を
  回帰テストで固定する」こと。本実装の `_notification_attrib` がそれ。
- **ADSTRANS の定数値は直感と逆。** SERVERONCHA=4、SERVERCYCLE=3。
  数値リテラルで書くと取り違える。必ず定数名で書く。
- **MCP SDK 2.x は破壊的変更だった。** クリーンな環境に無指定で入れると
  2.x が解決され、`mcp.server.fastmcp` が無く import 時点で落ちる
  (FastMCP は MCPServer に改名)。ピン留め (`mcp>=1.2,<2`) は必須で、
  かつ「新規環境では真っ先に import だけ通す」が有効なスモークテストになる。
- **`set_local_address` は Windows で使えない。** Linux 前提のサンプルを
  そのまま Windows に持ってくると刺さる。本実装ではプラットフォームで
  ガードし、Windows はルータ割当ての Net ID に任せる (§4.3)。

### 12.2 Notification の実務

- **「登録直後に現在値が 1 回飛ぶか」は実装依存。前提にするな。**
  モック初版が初回 tick で無変化イベントを飛ばし、テストが落ちて発覚した。
  本実装は二重に対策している: (a) サーバ側で on_change かつ前回記録値と
  同値の通知はイベント化しない、(b) モック側で登録時点の値を送信済み扱い。
  実機の TwinCAT は on_change で初期通知を送らない想定だが、
  サーバ側 dedupe があるためどちらでも壊れない。
- **parse は型が厳密。** コールバックの生データは `get_symbol` で得た
  plc_type (ctypes 型) で parse しないと復元できない。型不一致や想定外の
  データは例外になり、本実装ではコールバック内で握り潰している。
  つまり **parse 失敗は無言で消える**。観測が欠けたことに気づけないため、
  次版では parse_error のカウンタを `get_watch_status` に出すこと。
- **配列は Notification のコールバック復元に非対応 (pyads 仕様)。**
  構造体はバイト列で受けて自前デコードが必要。この制約が
  「配列・構造体は snapshot_only」という設計判断の根拠。
  逆に言えば、デバッグループの中心は基本型の変数で組むべきで、
  構造体を丸ごと見たい要求が出たら別ツール (構造体スナップショット) が要る。
- **ハンドル管理が一番壊れやすい場所。** 公式テストサーバの複数通知バグ
  (pyads issue #471) の原因もハンドル採番の持ち方だった。
  本実装の規約: user_handle はグループ内の通し番号、戻り handle は
  セッションに全件保持し、`stop_watch` で全件解除する。
  解除漏れは PLC 側に登録を残し、ルータの帯域と登録上限を食う。

### 12.3 接続共有とリトライの副作用 (対策を実装済み)

初版は「単一接続を全ツール・全 watch で共有」し、`ads_call` が
`ADSError` で一律に接続リセット + 1 回再試行していた。コード再読で
以下の副作用を確認し、0.4 で対策を実装した (§10 が現行仕様)。

確認していた副作用:
1. **恒常エラーでもリセットが走り、他の watch を巻き込む。**
   存在しない変数への `read_symbol` (ADSError 1808) でも接続が閉じられ、
   監視中 watch の Notification がすべて無効になるのに、セッションは
   active 表示のまま。「監視しているつもりで何も来ない」観測断絶。
2. **副作用のある処理への一律リトライは二重登録を生む。**
   `start_watch` の途中失敗で _do 全体が再実行され、二重登録が生じ得た。
3. **再接続しても watch は自動復旧しない** (上記 1 の裏側)。

実装した対策:
- エラーコードで分類する。恒常エラー (`PERMANENT_ADS_ERRORS`:
  1808 / 1793〜1797 / 1811 / 1815 / 1820 / 1821 / 1825 / 1827 / 1828) は
  リセットもリトライもしない。未知のコードは接続系に倒す
  (一過性の回復可能性を優先し、リトライは最大 1 回で有界)。
- リトライは読み取り系のみ。`start_watch` は `retry=False` で呼び、
  失敗時は部分登録の後始末 (best effort) をしてから送出する。
  呼び出し側 (Copilot) が start_watch をやり直す前提で、
  copilot-instructions.md にも state の見方を追記した。
- 接続リセット時は既存 watch をすべて `error` 状態へ遷移させ、
  `get_watch_events` / `get_watch_status` が `state` / `state_detail` /
  `warning` を返す。イベント履歴は保持する。コールバック側でも
  error 状態のセッションには追記しない。
- 回帰テスト 3 件で固定した: 恒常エラーで接続が差し替わらないこと /
  接続系エラーで読みが回復し watch が error になること /
  start_watch がリトライされないこと (呼び出し回数で検証)。

残る性質 (仕様として受け入れる):
- 接続系エラーが実際に起きた場合、その時点で張っていた watch は
  復旧しない。error 表示を見て張り直す運用が前提。
- 実機受入 (§11.2) では、監視中にわざと誤った変数名を読むテストを
  1 件追加し、watch が巻き込まれないことを実機でも確認すること。

### 12.4 フィルタ意味論の決定事項

- **deadband の基準は「最後に記録した値」。** 「最後に受信した値」基準だと、
  毎回 deadband 未満ずつ動く緩やかなドリフトが永遠にイベント化しない。
  記録値基準なら蓄積した変化が deadband を超えた時点で 1 件出る。
  テストで固定済み: 0.0 → 0.3 (抑制) → 0.6 (記録値 0.0 基準で通過)。
- **min_interval の時刻も記録時のみ更新する。** 抑制された通知で時刻を
  更新すると、チャタリングが続く間ずっと抑制され続ける。
- **抑制は「見せない」ではなく「数えて見せる」。** AI がイベントの欠落を
  「変化なし」と誤読しないよう、`suppressed` 件数を必ず返す。
  groups.yaml を調整すべき場面 (suppressed が異常に多い) もここから判断できる。

### 12.5 時刻の混在に注意

- イベントの `time` は PLC 側タイムスタンプ、baseline / snapshot の
  `read_at` はサーバ (AI 側 Windows) のローカル時刻。**時計の出所が違う**。
- 時系列の前後関係は同一 watch 内のイベント同士 (PLC 時計 + seq) で語ること。
  `read_at` とイベント時刻の差を「遅延」として解釈しない。
  copilot-instructions.md にもこの前提で読むよう書いてある。

### 12.6 モックで検証できること / できないこと

| 検証できた (モック + フェイク) | 検証できていない (実機でのみ) |
|---|---|
| MCP ハンドシェイク、ツール呼び出しの往復 | ADS プロトコル自体、ルート、TcAdsDll の挙動 |
| watch の登録 → イベント蓄積 → 差分取得 → 解除 | 実機 Notification の初期通知の有無、実タイムスタンプ |
| deadband / min_interval / 同値 dedupe の意味論 | cycle 周期の実精度、PLC タスクへの実負荷 |
| 読み取り専用ガード (書き込みが呼ばれたら落ちる) | 配列・構造体の baseline 実値、STRING の文字コード |
| エラー JSON の形 (不明グループ等) | 切断・再接続、§12.3 の巻き込みの実挙動 |

モックのシミュレーション粒度は 50ms tick で、時刻もクライアント時刻。
「モックで通った」はサーバ側ロジックの正しさまでで、通信の正しさは含まない。

### 12.7 開発・保守上の注意

- **FastMCP の `@mcp.tool()` は FunctionTool を返す。** 単体テストから
  ツール関数を直接呼ぶには `.fn` を剥がす必要がある
  (テスト側に `call()` ヘルパあり)。知らないと「呼べない」所で時間を溶かす。
- **stdio では stdout に何も書くな。** MCP の JSON-RPC が流れるため、
  デバッグ print 1 行で通信が壊れる。出力は stderr のみ。
- **設定の再読込は非対称。** `groups.yaml` は呼び出しごとに読み直す
  (再起動不要) が、`config.yaml` は import 時読み (net_id 等の変更は
  サーバ再起動が必要)。ハマりやすいので運用時は意識すること。
- **初回起動は温めておく。** `uv run` の初回は依存解決と venv 作成が走り、
  ネットワークも必要。現場で Copilot を開いてから初回解決が始まると
  体感が悪いので、投入前に一度起動しておく。
- **`Setup-AdsRouter-Check.ps1` は未実走。** Linux 側で書いたため
  構文レビューのみで、Windows 実機での実行確認は受入 (§11.2-1) で行う。

## 13. 既知の制約と将来拡張
- 次の改善候補は §12.2 の parse エラー可視化 (カウンタを status に出す)。
- 監視イベントはメモリ保持のみ。VS Code を閉じると消える。長期記録が必要になったら SQLite 永続化を追加する。
- 対象 PLC は単一 (config 単位)。複数台は MCP サーバを分けて登録する。
- 構造体の丸読み (read_structure_by_name 相当) は未提供。必要になったら structure_def を groups.yaml に持たせて追加する。
- MCP SDK は 1.x 固定。2.x 移行時は FastMCP → MCPServer の改名対応が必要。

## 14. ファイル一覧
```
.vscode/mcp.json                    Copilot の MCP 登録 (実機用 + モック用)
.github/copilot-instructions.md     デバッグ手順の指示
tools/ads-mcp/server.py             MCP サーバ本体
tools/ads-mcp/mock_plc.py           模擬 PLC (実機なし用)
tools/ads-mcp/config.yaml           接続・ルータ設定
tools/ads-mcp/groups.yaml           デバッグループ定義
tools/ads-mcp/smoke_test.py         実機疎通確認
tools/ads-mcp/windows/Setup-AdsRouter-Check.ps1  ルータ導入チェック
tools/ads-mcp/tests/                pytest 一式 + E2E スクリプト
SPEC.md (本書) / README.md          仕様と使い方
```

## 15. 参考
- pyads ドキュメント (Routing / Connections): Windows では TwinCAT Router UI でルートを張る作法、NotificationAttrib の仕様。
- Beckhoff Infosys: TC1000 (TwinCAT 3 ADS)、AMS ルート設定。
- NuGet: Beckhoff.TwinCAT.Ads (AdsRouterConsole / TcpRouter は .NET クライアント向けの代替ルータ)。
