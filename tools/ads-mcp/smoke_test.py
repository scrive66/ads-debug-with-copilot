#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ADS 疎通確認スクリプト (MCP 化の前に潰す用)

使い方:
    uv run python smoke_test.py <変数名>
    例: uv run python smoke_test.py MAIN.bTest

やること:
    1. config.yaml の net_id / port へ接続
    2. デバイス情報と ADS 状態を表示
    3. 指定変数を 1 回読む
    4. その変数に Notification を張り、10 秒間 変化を待って表示する

失敗する時は大体このどれか:
    - AMS Net ID / ポート違い
    - PLC 側にこの PC への ADS ルートが無い (要ルート登録)
    - ファイアウォールで TCP 48898 が閉じている
    - 変数がシンボルとして公開されていない (ダウンロード時のシンボル生成)
"""
import sys
import time
from pathlib import Path

import pyads
import yaml

BASE_DIR = Path(__file__).resolve().parent
config = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
NET_ID = config.get("net_id", "127.0.0.1.1.1")
PORT = int(config.get("port", 851))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    symbol = sys.argv[1]

    print(f"connecting: net_id={NET_ID} port={PORT}")
    plc = pyads.Connection(NET_ID, PORT)
    plc.open()
    try:
        name, version = plc.read_device_info()
        ads_state, device_state = plc.read_state()
        print(f"device: {name}  version: {version}  ads_state: {ads_state} ({device_state})")

        value = plc.read_by_name(symbol)
        print(f"read: {symbol} = {value!r}")

        sym = plc.get_symbol(symbol)
        print(f"info: type={getattr(sym, 'symbol_type', sym.plc_type)} comment={sym.comment!r}")

        print("notification test: 10 秒間、値の変化を待つ (実機側で値を変えてみること)")
        events = []

        def callback(notification, data):
            _h, ts, v = plc.parse_notification(notification, sym.plc_type)
            events.append((ts, v))
            print(f"  event: {ts} -> {v!r}")

        from ctypes import sizeof

        attr = pyads.NotificationAttrib(sizeof(sym.plc_type), pyads.ADSTRANS_SERVERONCHA, 0, 0)
        handle = plc.add_device_notification(symbol, attr, callback)
        try:
            time.sleep(10)
        finally:
            plc.del_device_notification(handle)
        print(f"notification events received: {len(events)}")
    finally:
        plc.close()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
