# Copilot 指示: TwinCAT PLC デバッグ (ADS 読み取り専用 MCP)

このプロジェクトでは MCP サーバ `twincat-ads` 経由で実機 PLC の変数を
**読み取り専用** で観測できる。書き込み手段は存在しないので、
値を変える提案や試行はしないこと (変更が必要な場合は人が PLC 側で行う)。

## デバッグの進め方

1. まず `plc_info` で接続とランタイム状態 (Run 等) を確認する。
2. 対象のデバッグループを `list_groups` で選び、`start_watch` で監視を開始する。
   - `start_watch` の戻りにある baseline が開始時点の全値。
   - groups.yaml の description / normal が「正常な状態」の定義。
     baseline と normal を照合して、既に異常な変数が無いか先に見る。
3. 現象の再現操作は人が行う。操作後に `get_watch_events` を呼び、
   変化イベントの時系列 (どの変数が、どの順で、どう変わったか) を読む。
   - **応答の `state` が `error` の watch は死んでいる** (接続リセットで
     Notification が無効化された)。イベントが来ないことを「変化なし」と
     解釈せず、`stop_watch` してから `start_watch` で張り直すこと。
4. イベントとソースコード (.TcPOU / .TcGVL 等) を照合し、
   「変わるべきなのに変わっていない変数」「想定外に変わった変数」を特定する。
5. 追加で必要な変数は `search_symbols` で探し、`read_symbol` /
   `get_symbol_info` で単発確認する。PLC コメントは仕様の手掛かりとして扱う。
6. 終わったら必ず `stop_watch` で監視を解除する。

## 注意

- イベントの時刻は PLC 側タイムスタンプ。順序の議論は seq と時刻で行う。
- `suppressed` が多い変数は deadband / min_interval で間引かれている。
  生の変化が必要なら、その旨を人に伝えて groups.yaml の調整を提案する
  (勝手に広い範囲を監視しない。監視は 1 グループずつが原則)。
- snapshot_only に回った変数 (配列・構造体) は開始時点の値しか無い。
  現在値が必要なら `get_group_snapshot` で読み直す。
- 値の解釈では型と単位を意識する。REAL の微小な差は誤差として扱い、
  エラーコード類はソース内の定義 (定数・ENUM) と突き合わせる。
- 推測で変数名を作らない。実在確認は `search_symbols` / `get_symbol_info` で行う。
