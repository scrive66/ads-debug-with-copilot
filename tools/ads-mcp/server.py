#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
twincat-ads-readonly MCP server
================================

TwinCAT ADS の「読み取り専用」MCP サーバ。

設計方針
--------
- 書き込みは *実装しない*。pyads の write_* はこのファイルのどこからも
  呼ばない。AI (GitHub Copilot) 経由では値を変える手段が存在しない。
- 監視は ADS Device Notification 前提 (ポーリングしない)。
  - 変化検出は PLC 側が行い、変化した時だけ通知が飛ぶので、
    ポーリングより PLC / ネットワーク双方の負荷が小さい。
  - アナログ値のように毎サイクル変わる変数は、グループ定義側で
    cycle モード (周期送信) か deadband (不感帯) を付けて抑える。
- スナップショット読み (read_list_by_name) は watch 開始時の
  ベースライン取得と、単発の掘り下げ専用。
- コールバック (ADS DLL のスレッドから呼ばれる) の中では ADS を
  呼ばない。parse してメモリ上のリングバッファに積むだけにする。

起動は .vscode/mcp.json から stdio で行う想定。
設定は同ディレクトリの config.yaml / groups.yaml を参照。

接続前提 (AI 側 Windows):
    AI 側 Windows には ADS ルータを入れる。本プロジェクトの標準は
    TC1000 (TwinCAT 3 ADS Setup) で、TcAdsDll と AMS ルータが入り、
    ルートは TwinCAT Router UI (トレイ > Router > Edit Routes) で張る。
    pyads (Windows) はこのローカルルータ経由で通信する。

実機なしモード:
    環境変数 ADS_MCP_BACKEND=mock で模擬 PLC (mock_plc.py) に接続し、
    全ツールを実機なしで動かせる。Copilot の動作確認・練習用。
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyads
import yaml
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# 定数 (安全側の上限)
# ---------------------------------------------------------------------------
MAX_SYMBOLS_PER_WATCH = 100
MAX_ACTIVE_WATCHES = 4
MAX_EVENTS_PER_WATCH = 5000
MAX_SEARCH_RESULTS = 200

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 設定読み込み
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CONFIG = _load_yaml(BASE_DIR / "config.yaml")
ADS_NET_ID = os.environ.get("ADS_MCP_NET_ID", CONFIG.get("net_id", "127.0.0.1.1.1"))
ADS_PORT = int(os.environ.get("ADS_MCP_PORT", CONFIG.get("port", 851)))
LOCAL_NET_ID = os.environ.get("ADS_MCP_LOCAL_NET_ID", CONFIG.get("local_net_id"))
BACKEND = os.environ.get("ADS_MCP_BACKEND", CONFIG.get("backend", "pyads")).lower()
ROUTER_CFG = CONFIG.get("router", {}) or {}
GROUPS_FILE = Path(
    os.environ.get("ADS_MCP_GROUPS", str(BASE_DIR / "groups.yaml"))
)


def load_groups() -> dict[str, Any]:
    """groups.yaml を読む。ツール呼び出しのたびに読むので再起動なしで編集可。"""
    data = _load_yaml(GROUPS_FILE)
    return data.get("groups", {}) or {}


# ---------------------------------------------------------------------------
# 値の JSON 化
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, BaseException):
        return {"error": str(value)}
    return str(value)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# ADS 接続 (単一接続を全ツールで共有。ADS 呼び出しはロックで直列化)
# ---------------------------------------------------------------------------


class AdsClient:
    def __init__(self) -> None:
        self._conn: pyads.Connection | None = None
        self.lock = threading.RLock()

    def _make_connection(self):
        """新しい接続を作って開く。テストではここを差し替えられる。"""
        if BACKEND == "mock":
            from mock_plc import MockPlc

            conn = MockPlc()
            conn.open()
            return conn
        conn = pyads.Connection(ADS_NET_ID, ADS_PORT)
        if LOCAL_NET_ID and sys.platform != "win32":
            # set_local_address は Windows では使えない (ルータが割り当てる)。
            # Windows では TC1000 のローカルルータが自機 Net ID を持つので
            # ここは Linux 等のルータレス構成専用。
            pyads.set_local_address(LOCAL_NET_ID)
        conn.open()
        return conn

    def ensure_open(self):
        with self.lock:
            if self._conn is not None and self._conn.is_open:
                return self._conn
            self._conn = self._make_connection()
            return self._conn

    def reset(self) -> None:
        with self.lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


PLC = AdsClient()

# ---------------------------------------------------------------------------
# ADS エラー分類 (SPEC §12.3 の対策)
#
# 恒常エラー: リクエスト自体が誤っている / 対象が確定的に拒否した。
#   接続は健全なので、リセットもリトライもしない (他 watch を巻き込まない)。
# 接続系エラー: タイムアウト、対象未達、ポート未開など。
#   接続リセットが回復策になり得るが、その接続で張った Notification は
#   すべて無効になるため、既存 watch を error 状態へ遷移させる。
# 未知のコードは接続系として扱う (一過性に倒す。リトライは最大 1 回)。
# ---------------------------------------------------------------------------
PERMANENT_ADS_ERRORS = {
    1793,  # service not supported
    1794,  # invalid index group
    1795,  # invalid index offset
    1796,  # reading/writing not permitted
    1797,  # parameter size not correct
    1808,  # symbol not found
    1811,  # AdsTransMode not supported
    1815,  # size for watch too big
    1820,  # class ID invalid
    1821,  # object ID invalid
    1825,  # invalid array index
    1827,  # access denied
    1828,  # missing license
}


def _is_permanent_ads_error(exc: pyads.ADSError) -> bool:
    return getattr(exc, "err_code", None) in PERMANENT_ADS_ERRORS


def ads_call(fn, retry: bool = True):
    """ADS 呼び出しをロック内で実行する。

    - 恒常エラー: そのまま送出 (リセット・リトライなし)。
    - 接続系エラー: 既存 watch を error 状態にしてから接続をリセットし、
      retry=True (読み取り系) の場合のみ 1 回だけ再試行する。
      retry=False は start_watch のような副作用のある処理用。
    """
    with PLC.lock:
        try:
            return fn(PLC.ensure_open())
        except pyads.ADSError as exc:
            if _is_permanent_ads_error(exc):
                raise
            _mark_all_watches_broken(f"connection reset by ADSError: {exc}")
            PLC.reset()
            if not retry:
                raise
            return fn(PLC.ensure_open())


# ---------------------------------------------------------------------------
# Notification 監視セッション
# ---------------------------------------------------------------------------


@dataclass
class SymbolWatch:
    name: str
    plc_type: Any  # ctypes type
    mode: str = "on_change"  # on_change | cycle
    cycle_ms: int = 1000
    deadband: float = 0.0
    min_interval_ms: int = 0
    last_recorded: Any = None
    last_event_at: float = 0.0  # monotonic
    suppressed: int = 0


@dataclass
class WatchSession:
    watch_id: str
    group: str
    started_at: str
    state: str = "active"  # active | error (接続リセットで Notification が無効化された)
    state_detail: str | None = None
    symbols: dict[int, SymbolWatch] = field(default_factory=dict)  # user_handle -> watch
    handles: list[tuple] = field(default_factory=list)  # add_device_notification の戻り
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_WATCH))
    seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    current: dict[str, Any] = field(default_factory=dict)  # name -> 最新値
    baseline: dict[str, Any] = field(default_factory=dict)
    snapshot_only: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


WATCHES: dict[str, WatchSession] = {}
WATCHES_LOCK = threading.Lock()
_watch_counter = itertools.count(1)


def _mark_all_watches_broken(reason: str) -> None:
    """接続リセットに伴い、既存 watch をすべて error 状態にする。

    Notification は接続と共に無効になるため、そのまま放置すると
    「監視中のはずが何も来ない」観測断絶になる。イベント履歴は残す。
    """
    with WATCHES_LOCK:
        sessions = list(WATCHES.values())
    for s in sessions:
        with s.lock:
            if s.state == "active":
                s.state = "error"
                s.state_detail = reason


def _passes_filter(sw: SymbolWatch, value: Any) -> bool:
    """deadband / min_interval の判定。通過しなければ suppressed を数える。"""
    now = time.monotonic()
    if sw.min_interval_ms > 0 and sw.last_recorded is not None:
        if (now - sw.last_event_at) * 1000 < sw.min_interval_ms:
            sw.suppressed += 1
            return False
    if (
        sw.deadband > 0
        and isinstance(value, (int, float))
        and isinstance(sw.last_recorded, (int, float))
    ):
        if abs(value - sw.last_recorded) < sw.deadband:
            sw.suppressed += 1
            return False
    return True


def _make_callback(session: WatchSession):
    """ADS DLL スレッドから呼ばれるコールバックを生成する。

    ここで ADS を呼ぶとデッドロックし得るので、parse とバッファ追加だけ。
    """

    def callback(notification, data):  # data = user_handle (int)
        if session.state != "active":
            return  # 接続リセット済みセッションの残留通知は捨てる
        sw = session.symbols.get(data)
        if sw is None:
            return
        try:
            _handle, timestamp, value = PLC._conn.parse_notification(
                notification, sw.plc_type
            )
        except Exception:
            return
        value = to_jsonable(value)
        with session.lock:
            session.current[sw.name] = value
            # on_change では「前回記録値と同じ」通知はイベントにしない
            # (登録直後の初期通知など、実機/モック差異の吸収)
            if (
                sw.mode == "on_change"
                and sw.last_recorded is not None
                and value == sw.last_recorded
            ):
                return
            if not _passes_filter(sw, value):
                return
            sw.last_recorded = value
            sw.last_event_at = time.monotonic()
            ts = (
                timestamp.isoformat(timespec="milliseconds")
                if isinstance(timestamp, datetime)
                else now_iso()
            )
            session.events.append(
                {
                    "seq": next(session.seq),
                    "time": ts,
                    "symbol": sw.name,
                    "value": value,
                }
            )

    return callback


def _notification_attrib(sw: SymbolWatch):
    """NotificationAttrib の max_delay / cycle_time 引数は *ミリ秒* で渡す。

    pyads 内部で x10000 して 100ns 単位 (ADS の実単位) に変換される。
    ここで先に x10000 すると周期が 10000 倍に化けるので絶対にしない。
    """
    from ctypes import sizeof

    length = sizeof(sw.plc_type)
    if sw.mode == "cycle":
        return pyads.NotificationAttrib(
            length,
            pyads.ADSTRANS_SERVERCYCLE,
            0,
            float(sw.cycle_ms),
        )
    # on_change: 変化時のみ送信。max_delay=0 は構造体の既定 (束ねなし)
    return pyads.NotificationAttrib(length, pyads.ADSTRANS_SERVERONCHA, 0, 0)


# ---------------------------------------------------------------------------
# MCP サーバ
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "twincat-ads-readonly",
    instructions=(
        "TwinCAT PLC の変数を読み取り専用で観測するツール群。"
        "書き込み手段は存在しない。"
        "デバッグは list_groups -> start_watch の順で始め、"
        "get_watch_events で変化イベントを確認すること。"
        "現在値の単発確認は get_group_snapshot / read_symbol を使う。"
    ),
)


@mcp.tool()
def plc_info() -> dict:
    """対象 PLC のデバイス情報と ADS 状態を返す。接続確認用。"""
    def _do(conn):
        name, version = conn.read_device_info()
        ads_state, device_state = conn.read_state()
        return {
            "backend": BACKEND,
            "net_id": ADS_NET_ID,
            "port": ADS_PORT,
            "router": ROUTER_CFG,
            "device_name": str(name),
            "version": f"{version.major}.{version.minor}.{version.build}",
            "ads_state": int(ads_state),
            "ads_state_name": str(ads_state),
            "device_state": int(device_state),
        }

    return ads_call(_do)


@mcp.tool()
def list_groups() -> dict:
    """groups.yaml に定義されたデバッグループの一覧を返す。"""
    groups = load_groups()
    return {
        "groups": [
            {
                "name": name,
                "description": g.get("description", ""),
                "symbol_count": len(g.get("symbols", [])),
            }
            for name, g in groups.items()
        ]
    }


@mcp.tool()
def get_group_snapshot(group: str) -> dict:
    """グループ内の全変数の現在値を一括で読む (ADS Sum Read)。時刻付き。"""
    groups = load_groups()
    if group not in groups:
        return {"error": f"unknown group: {group}", "known": list(groups.keys())}
    names = [s["name"] for s in groups[group].get("symbols", [])]
    values = ads_call(lambda conn: conn.read_list_by_name(names))
    return {
        "group": group,
        "read_at": now_iso(),
        "values": {k: to_jsonable(v) for k, v in values.items()},
    }


@mcp.tool()
def read_symbol(name: str) -> dict:
    """単一変数の現在値を読む。グループ外を掘り下げる時用。"""
    value = ads_call(lambda conn: conn.read_by_name(name))
    return {"symbol": name, "read_at": now_iso(), "value": to_jsonable(value)}


@mcp.tool()
def get_symbol_info(name: str) -> dict:
    """変数の型・PLC コメントなどを返す。コメントは仕様の手掛かりになる。"""
    def _do(conn):
        sym = conn.get_symbol(name)
        return {
            "name": sym.name,
            "type": getattr(sym, "symbol_type", None) or str(sym.plc_type),
            "comment": sym.comment,
            "index_group": sym.index_group,
            "index_offset": sym.index_offset,
        }

    return ads_call(_do)


@mcp.tool()
def search_symbols(pattern: str) -> dict:
    """シンボル名に pattern を含む変数を検索する (大文字小文字を区別しない)。"""
    def _do(conn):
        pat = pattern.lower()
        hits = [
            {
                "name": s.name,
                "type": getattr(s, "symbol_type", None) or str(s.plc_type),
                "comment": s.comment,
            }
            for s in conn.get_all_symbols()
            if pat in s.name.lower()
        ]
        return {"pattern": pattern, "count": len(hits), "symbols": hits[:MAX_SEARCH_RESULTS]}

    return ads_call(_do)


@mcp.tool()
def start_watch(group: str) -> dict:
    """グループの監視を開始する (ADS Notification)。

    - 開始時点の全値が baseline として返る。
    - 以後の変化はサーバ内に蓄積され、get_watch_events で取得する。
    - 配列・構造体・WSTRING など Notification 非対応の型は
      snapshot_only に回され、baseline のみ対象になる。
    """
    groups = load_groups()
    if group not in groups:
        return {"error": f"unknown group: {group}", "known": list(groups.keys())}
    with WATCHES_LOCK:
        if len(WATCHES) >= MAX_ACTIVE_WATCHES:
            return {"error": f"too many active watches (max {MAX_ACTIVE_WATCHES}). stop_watch first."}
        watch_id = f"w{next(_watch_counter)}"

    gdef = groups[group]
    entries = gdef.get("symbols", [])
    if len(entries) > MAX_SYMBOLS_PER_WATCH:
        return {"error": f"group has {len(entries)} symbols, limit is {MAX_SYMBOLS_PER_WATCH}"}

    session = WatchSession(watch_id=watch_id, group=group, started_at=now_iso())
    callback = _make_callback(session)

    def _do(conn):
        # 1) ベースラインを一括読み
        names = [e["name"] for e in entries]
        values = conn.read_list_by_name(names)
        session.baseline = {k: to_jsonable(v) for k, v in values.items()}
        session.current = dict(session.baseline)

        # 2) 変数ごとに Notification を登録
        for idx, entry in enumerate(entries):
            name = entry["name"]
            try:
                sym = conn.get_symbol(name)
                plc_type = sym.plc_type
                type_name = getattr(sym, "symbol_type", "") or ""
                if plc_type is None or "WSTRING" in type_name.upper():
                    raise ValueError(f"notification unsupported type: {type_name}")
                sw = SymbolWatch(
                    name=name,
                    plc_type=plc_type,
                    mode=entry.get("mode", gdef.get("mode", "on_change")),
                    cycle_ms=int(entry.get("cycle_ms", gdef.get("cycle_ms", 1000))),
                    deadband=float(entry.get("deadband", 0.0)),
                    min_interval_ms=int(entry.get("min_interval_ms", 0)),
                )
                if name in session.baseline:
                    sw.last_recorded = session.baseline[name]
                session.symbols[idx] = sw
                handle = conn.add_device_notification(
                    name, _notification_attrib(sw), callback, user_handle=idx
                )
                session.handles.append(handle)
            except Exception as exc:  # 登録できない変数は snapshot_only に落とす
                session.snapshot_only.append(name)
                session.errors.append(f"{name}: {exc}")
        return True

    try:
        # 副作用のある処理なので ads_call の自動リトライ対象外 (SPEC §12.3)。
        # 失敗したら Copilot / 人が start_watch をやり直すのが安全。
        ads_call(_do, retry=False)
    except Exception:
        # 部分的に登録できた Notification を PLC 側に残さない (best effort)
        try:
            conn = PLC.ensure_open()
            for h in session.handles:
                try:
                    conn.del_device_notification(h)
                except Exception:
                    pass
        except Exception:
            pass
        raise
    with WATCHES_LOCK:
        WATCHES[watch_id] = session
    return {
        "watch_id": watch_id,
        "group": group,
        "state": session.state,
        "started_at": session.started_at,
        "watching": [sw.name for sw in session.symbols.values()],
        "snapshot_only": session.snapshot_only,
        "errors": session.errors,
        "baseline": session.baseline,
        "note": "変化イベントは get_watch_events で取得。終わったら stop_watch。",
    }


@mcp.tool()
def get_watch_events(watch_id: str, after_seq: int = 0, limit: int = 500) -> dict:
    """監視中の変化イベントを返す。after_seq より後のイベントだけを返す。"""
    session = WATCHES.get(watch_id)
    if session is None:
        return {"error": f"unknown watch_id: {watch_id}", "active": list(WATCHES.keys())}
    with session.lock:
        events = [e for e in session.events if e["seq"] > after_seq][:limit]
        out = {
            "watch_id": watch_id,
            "group": session.group,
            "state": session.state,
            "fetched_at": now_iso(),
            "events": events,
            "event_count_total": len(session.events),
            "suppressed": {
                sw.name: sw.suppressed
                for sw in session.symbols.values()
                if sw.suppressed
            },
            "current": dict(session.current),
        }
        if session.state == "error":
            out["state_detail"] = session.state_detail
            out["warning"] = (
                "接続リセットにより Notification は無効化された。"
                "これ以降の変化は記録されないため、start_watch で張り直すこと。"
            )
        return out


@mcp.tool()
def get_watch_status(watch_id: str) -> dict:
    """監視セッションの状態 (現在値・イベント数・抑制数) を返す。"""
    session = WATCHES.get(watch_id)
    if session is None:
        return {"error": f"unknown watch_id: {watch_id}", "active": list(WATCHES.keys())}
    with session.lock:
        return {
            "watch_id": watch_id,
            "group": session.group,
            "state": session.state,
            "state_detail": session.state_detail,
            "started_at": session.started_at,
            "watching": [sw.name for sw in session.symbols.values()],
            "snapshot_only": session.snapshot_only,
            "event_count": len(session.events),
            "suppressed": {
                sw.name: sw.suppressed
                for sw in session.symbols.values()
                if sw.suppressed
            },
            "current": dict(session.current),
        }


@mcp.tool()
def stop_watch(watch_id: str) -> dict:
    """監視を終了し、Notification を解除する。"""
    with WATCHES_LOCK:
        session = WATCHES.pop(watch_id, None)
    if session is None:
        return {"error": f"unknown watch_id: {watch_id}", "active": list(WATCHES.keys())}

    def _do(conn):
        for handle in session.handles:
            try:
                conn.del_device_notification(handle)
            except Exception:
                pass
        return True

    try:
        ads_call(_do)
    except Exception as exc:
        session.errors.append(f"cleanup: {exc}")
    return {
        "watch_id": watch_id,
        "group": session.group,
        "event_count": len(session.events),
        "stopped_at": now_iso(),
    }


if __name__ == "__main__":
    mcp.run()  # stdio
