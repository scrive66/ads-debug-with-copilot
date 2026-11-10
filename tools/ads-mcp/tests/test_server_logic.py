# -*- coding: utf-8 -*-
"""
実機なしテストスイート。

- FakeConnection テスト: watch の登録・deadband・min_interval・解除、
  NotificationAttrib の周期換算 (回帰)、書き込みが呼ばれないこと。
- Mock バックエンドテスト: 模擬 PLC で start_watch -> イベント発生 ->
  stop_watch の実フロー。

実行:  uv run --with pytest python -m pytest tests/ -q
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("ADS_MCP_GROUPS", str(BASE / "groups.yaml"))

import pyads  # noqa: E402
import server  # noqa: E402


def call(fn, **kw):
    return getattr(fn, "fn", fn)(**kw)


# ---------------------------------------------------------------------------
# NotificationAttrib の換算 (pyads は ms で受けて内部で x10000 する)
# ---------------------------------------------------------------------------
def test_attrib_cycle_conversion():
    sw = server.SymbolWatch(
        name="X.f", plc_type=pyads.PLCTYPE_LREAL, mode="cycle", cycle_ms=200
    )
    attr = server._notification_attrib(sw)
    assert attr.trans_mode == pyads.ADSTRANS_SERVERCYCLE
    assert attr.cycle_time == 200 * 10000  # ADS 実単位 (100ns)

    sw2 = server.SymbolWatch(name="X.b", plc_type=pyads.PLCTYPE_BOOL)
    attr2 = server._notification_attrib(sw2)
    assert attr2.trans_mode == pyads.ADSTRANS_SERVERONCHA


# ---------------------------------------------------------------------------
# FakeConnection による watch ロジック
# ---------------------------------------------------------------------------
TYPES = {
    "MAIN.bFlag": (pyads.PLCTYPE_BOOL, "BOOL"),
    "MAIN.fPos": (pyads.PLCTYPE_LREAL, "LREAL"),
}
VALUES = {"MAIN.bFlag": False, "MAIN.fPos": 0.0}


class FakeConn:
    is_open = True

    def __init__(self):
        self.callbacks = {}
        self.deleted = []
        self.read_error = None  # err_code を入れると read_by_name が ADSError を投げる

    def read_by_name(self, name, *a, **k):
        if self.read_error is not None:
            raise pyads.ADSError(self.read_error, "injected")
        if name not in VALUES:
            raise pyads.ADSError(1808, "symbol not found")
        return VALUES[name]

    def read_list_by_name(self, names):
        return {n: VALUES[n] for n in names}

    def get_symbol(self, name):
        t, tn = TYPES[name]
        return SimpleNamespace(
            name=name, comment="c", index_group=16448, index_offset=0,
            plc_type=t, symbol_type=tn,
        )

    def add_device_notification(self, name, attr, callback, user_handle=None):
        self.callbacks[user_handle] = callback
        return (999, user_handle)

    def del_device_notification(self, handle):
        self.deleted.append(handle)

    def parse_notification(self, notification, plc_type):
        return notification

    # 書き込みが呼ばれたら即失敗 (読み取り専用の回帰ガード)
    def write_by_name(self, *a, **k):
        raise AssertionError("write called!")

    def write_list_by_name(self, *a, **k):
        raise AssertionError("write called!")


@pytest.fixture()
def fake_groups(tmp_path, monkeypatch):
    g = tmp_path / "groups.yaml"
    g.write_text(
        """
groups:
  testgrp:
    description: "t"
    symbols:
      - name: "MAIN.bFlag"
      - name: "MAIN.fPos"
        mode: cycle
        cycle_ms: 200
        deadband: 0.5
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "GROUPS_FILE", g)
    fake = FakeConn()
    server.PLC._conn = fake
    yield fake
    server.PLC._conn = None
    server.WATCHES.clear()


def test_watch_deadband_and_cleanup(fake_groups):
    fake = fake_groups
    res = call(server.start_watch, group="testgrp")
    assert set(res["watching"]) == {"MAIN.bFlag", "MAIN.fPos"}
    wid = res["watch_id"]
    now = datetime.now()
    fake.callbacks[0]((0, now, True), 0)          # BOOL 変化 -> イベント
    fake.callbacks[1]((0, now, 0.3), 1)           # deadband 0.5 未満 -> 抑制
    fake.callbacks[1]((0, now, 0.6), 1)           # 記録値 0.0 基準で通過
    ev = call(server.get_watch_events, watch_id=wid)
    assert [(e["symbol"], e["value"]) for e in ev["events"]] == [
        ("MAIN.bFlag", True),
        ("MAIN.fPos", 0.6),
    ]
    assert ev["suppressed"].get("MAIN.fPos") == 1
    call(server.stop_watch, watch_id=wid)
    assert fake.deleted, "del_device_notification が呼ばれていない"


def test_unknown_group():
    assert "error" in call(server.start_watch, group="no_such_group")


# ---------------------------------------------------------------------------
# SPEC §12.3 対策: エラー分類 / リトライ限定 / watch の error 遷移
# ---------------------------------------------------------------------------
def test_permanent_error_no_reset(fake_groups):
    """1808 (シンボル不存在) では接続をリセットせず、watch も巻き込まない。"""
    fake = fake_groups
    res = call(server.start_watch, group="testgrp")
    wid = res["watch_id"]
    with pytest.raises(pyads.ADSError):
        call(server.read_symbol, name="MAIN.no_such")
    assert server.PLC._conn is fake  # リセットされていない
    st = call(server.get_watch_status, watch_id=wid)
    assert st["state"] == "active"


def test_connection_error_marks_watch_broken_and_recovers(fake_groups, monkeypatch):
    """接続系エラー (1861) では読み取りは再試行で回復し、既存 watch は error になる。"""
    fake1 = fake_groups
    res = call(server.start_watch, group="testgrp")
    wid = res["watch_id"]
    fake1.read_error = 1861  # timeout elapsed
    fake2 = FakeConn()
    monkeypatch.setattr(server.PLC, "_make_connection", lambda: fake2)

    val = call(server.read_symbol, name="MAIN.bFlag")
    assert val["value"] is False  # fake2 で再試行成功
    assert server.PLC._conn is fake2

    st = call(server.get_watch_status, watch_id=wid)
    assert st["state"] == "error" and st["state_detail"]
    ev = call(server.get_watch_events, watch_id=wid)
    assert ev["state"] == "error" and "warning" in ev


def test_start_watch_not_retried(monkeypatch, tmp_path):
    """start_watch は副作用があるため、接続系エラーでも自動リトライしない。"""
    g = tmp_path / "groups.yaml"
    g.write_text(
        'groups:\n  testgrp:\n    description: "t"\n    symbols:\n      - name: "MAIN.bFlag"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "GROUPS_FILE", g)
    server.WATCHES.clear()

    class FailListConn(FakeConn):
        def __init__(self):
            super().__init__()
            self.list_calls = 0

        def read_list_by_name(self, names):
            self.list_calls += 1
            raise pyads.ADSError(1861, "timeout injected")

    conn = FailListConn()
    server.PLC._conn = conn
    monkeypatch.setattr(server.PLC, "_make_connection", lambda: conn)
    try:
        with pytest.raises(pyads.ADSError):
            call(server.start_watch, group="testgrp")
        assert conn.list_calls == 1  # リトライされていない
        assert not server.WATCHES  # セッションも残っていない
    finally:
        server.PLC._conn = None
        server.WATCHES.clear()


# ---------------------------------------------------------------------------
# Mock バックエンド (模擬 PLC で実フロー)
# ---------------------------------------------------------------------------
def test_mock_backend_watch_flow(monkeypatch):
    monkeypatch.setattr(server, "BACKEND", "mock")
    server.PLC.reset()
    try:
        info = call(server.plc_info)
        assert info["device_name"].startswith("MockPLC")
        assert info["ads_state"] == 5

        res = call(server.start_watch, group="example_axis")
        assert res["watching"], res
        wid = res["watch_id"]
        time.sleep(2.5)
        ev = call(server.get_watch_events, watch_id=wid)
        pos_events = [
            e for e in ev["events"]
            if e["symbol"] in ("MAIN.stAxis1.fActPosition", "MAIN.stAxis1.fSetPosition")
        ]
        assert len(pos_events) >= 3, ev["events"]
        # bError はシナリオ OFF では変化しないのでイベントに出ない
        assert not [e for e in ev["events"] if e["symbol"] == "MAIN.stAxis1.bError"]
        call(server.stop_watch, watch_id=wid)
    finally:
        server.PLC.reset()
        monkeypatch.setattr(server, "BACKEND", "pyads")
