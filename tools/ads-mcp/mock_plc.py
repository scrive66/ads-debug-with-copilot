#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MockPlc: 実機なしで MCP サーバ全体を動かすための模擬 PLC。

server.py が使う pyads.Connection のメソッドだけを実装する。
バックグラウンドスレッドで軸モデルを回し、登録された Notification へ
本物と同じ形式 (callback(notification, user_handle)) で変化を届ける。

模擬している変数は groups.yaml のサンプル (example_axis / example_io) と一致:
    MAIN.stAxis1.* / GVL_IO.*

環境変数 MOCK_ERROR_SCENARIO=1 で、60 秒周期で 5 秒間だけ
bError=TRUE / nErrorId=13313 (16#3401) になる異常シナリオが有効になる。

注意: これは MCP サーバ側の動作確認・Copilot の使い方練習用。
実機の ADS プロトコルそのものの検証にはならない (それは smoke_test.py で)。
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pyads

# name -> (plc_type, type_name, comment, 初期値)
SYMBOLS: dict[str, tuple[Any, str, str, Any]] = {
    "MAIN.stAxis1.bError": (pyads.PLCTYPE_BOOL, "BOOL", "軸1 エラー有無", False),
    "MAIN.stAxis1.nErrorId": (pyads.PLCTYPE_INT, "INT", "軸1 エラーID (0=正常)", 0),
    "MAIN.stAxis1.bMoving": (pyads.PLCTYPE_BOOL, "BOOL", "軸1 動作中", False),
    "MAIN.stAxis1.fActPosition": (pyads.PLCTYPE_LREAL, "LREAL", "軸1 実位置 [mm]", 10.0),
    "MAIN.stAxis1.fSetPosition": (pyads.PLCTYPE_LREAL, "LREAL", "軸1 指令位置 [mm]", 10.0),
    "GVL_IO.bEmergencyStop": (pyads.PLCTYPE_BOOL, "BOOL", "非常停止 (TRUE=押下)", False),
    "GVL_IO.bSafetyDoorOpen": (pyads.PLCTYPE_BOOL, "BOOL", "安全扉 開", False),
}

TICK_SEC = 0.05


class _Registration:
    def __init__(self, name, attrib, callback, user_handle):
        self.name = name
        self.callback = callback
        self.user_handle = user_handle
        self.trans_mode = attrib.trans_mode
        # attrib.cycle_time は 100ns 単位で返る
        self.cycle_ms = (attrib.cycle_time or 0) / 10000.0
        self.last_sent: Any = None
        self.last_sent_at = 0.0  # monotonic


class MockPlc:
    def __init__(self) -> None:
        self._values = {name: spec[3] for name, spec in SYMBOLS.items()}
        self._lock = threading.Lock()
        self._regs: dict[tuple, _Registration] = {}
        self._open = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = time.monotonic()
        self.error_scenario = os.environ.get("MOCK_ERROR_SCENARIO", "") == "1"

    # -- 接続 ---------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._simulate, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._open = False

    # -- デバイス情報 ---------------------------------------------------------
    def read_device_info(self):
        version = SimpleNamespace(major=3, minor=1, build=4024)
        return "MockPLC (simulated, no hardware)", version

    def read_state(self):
        return 5, 0  # ADS state 5 = Run

    # -- 読み取り -------------------------------------------------------------
    def _check(self, name: str) -> None:
        if name not in SYMBOLS:
            raise pyads.ADSError(1808, f"symbol not found: {name}")

    def read_by_name(self, name, *args, **kwargs):
        self._check(name)
        with self._lock:
            return self._values[name]

    def read_list_by_name(self, names, *args, **kwargs):
        with self._lock:
            out = {}
            for n in names:
                self._check(n)
                out[n] = self._values[n]
            return out

    def get_symbol(self, name, *args, **kwargs):
        self._check(name)
        plc_type, type_name, comment, _ = SYMBOLS[name]
        idx = list(SYMBOLS).index(name)
        return SimpleNamespace(
            name=name,
            plc_type=plc_type,
            symbol_type=type_name,
            comment=comment,
            index_group=16448,
            index_offset=idx * 16,
        )

    def get_all_symbols(self):
        return [self.get_symbol(n) for n in SYMBOLS]

    # -- Notification -----------------------------------------------------------
    def add_device_notification(self, name, attrib, callback, user_handle=None):
        self._check(name)
        handle = (len(self._regs) + 1, user_handle)
        with self._lock:
            reg = _Registration(name, attrib, callback, user_handle)
            # 実機の on_change と同じく、登録時点の値は「送信済み」として扱い
            # 初回 tick で無変化イベントを飛ばさない
            reg.last_sent = self._values[name]
            reg.last_sent_at = time.monotonic()
            self._regs[handle] = reg
        return handle

    def del_device_notification(self, handle) -> None:
        with self._lock:
            self._regs.pop(tuple(handle), None)

    def parse_notification(self, notification, plc_type):
        # server 側コールバックへ (handle, timestamp, value) をそのまま返す
        return notification

    # -- 書き込みは模擬でも禁止 (このプロジェクトは読み取り専用) -------------------
    def write_by_name(self, *args, **kwargs):
        raise RuntimeError("read-only project: write_by_name is disabled")

    def write_list_by_name(self, *args, **kwargs):
        raise RuntimeError("read-only project: write_list_by_name is disabled")

    # -- シミュレーション ---------------------------------------------------------
    def _simulate(self) -> None:
        while not self._stop.wait(TICK_SEC):
            t = time.monotonic() - self._t0
            with self._lock:
                v = self._values
                setpos = 50.0 + 40.0 * math.sin(t / 6.0)
                v["MAIN.stAxis1.fSetPosition"] = round(setpos, 4)
                act = v["MAIN.stAxis1.fActPosition"]
                v["MAIN.stAxis1.fActPosition"] = round(act + (setpos - act) * 0.12, 4)
                v["MAIN.stAxis1.bMoving"] = abs(setpos - v["MAIN.stAxis1.fActPosition"]) > 0.05
                if self.error_scenario:
                    in_error = (t % 60.0) >= 30.0 and (t % 60.0) < 35.0
                    v["MAIN.stAxis1.bError"] = in_error
                    v["MAIN.stAxis1.nErrorId"] = 13313 if in_error else 0
                v["GVL_IO.bSafetyDoorOpen"] = (t % 25.0) < 2.0
                snapshot = dict(v)
                regs = list(self._regs.values())

            now = time.monotonic()
            for reg in regs:
                value = snapshot[reg.name]
                if reg.trans_mode == pyads.ADSTRANS_SERVERCYCLE:
                    if (now - reg.last_sent_at) * 1000.0 < max(reg.cycle_ms, 1.0):
                        continue
                else:  # SERVERONCHA 等は変化時のみ
                    if reg.last_sent is not None and reg.last_sent == value:
                        continue
                reg.last_sent = value
                reg.last_sent_at = now
                try:
                    reg.callback((0, datetime.now(), value), reg.user_handle)
                except Exception:
                    pass
