# -*- coding: utf-8 -*-
"""
モックバックエンドでの MCP E2E 確認スクリプト (実機なし)。

実際の MCP クライアントとして stdio で server.py を起動し、
plc_info -> start_watch -> (3 秒待機) -> get_watch_events -> stop_watch
を通す。Copilot から使う時と同じ経路の確認になる。

実行:  uv run python tests/e2e_mock_mcp.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).resolve().parent.parent / "server.py"


async def main() -> None:
    env = dict(os.environ, ADS_MCP_BACKEND="mock")
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def call(name, args=None):
                r = await s.call_tool(name, args or {})
                return json.loads(r.content[0].text)

            info = await call("plc_info")
            print("plc_info:", info["device_name"], "| backend:", info["backend"])
            res = await call("start_watch", {"group": "example_axis"})
            print("start_watch:", res["watch_id"], res["watching"])
            await asyncio.sleep(3)
            ev = await call("get_watch_events", {"watch_id": res["watch_id"]})
            print(f"events: {len(ev['events'])} (suppressed: {ev['suppressed']})")
            for e in ev["events"][:5]:
                print("  ", e["seq"], e["symbol"], "=", e["value"])
            assert len(ev["events"]) >= 3
            print("stop_watch:", await call("stop_watch", {"watch_id": res["watch_id"]}))
            print("E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
