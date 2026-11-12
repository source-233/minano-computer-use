"""Real MCP stdio initialization, schema discovery and read-only tool smoke."""

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    artifacts = root / ".artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "minano_computer_use.server"],
        cwd=str(root),
    )
    with (artifacts / "stdio-server.stderr.log").open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=15),
            ) as client:
                initialized = await client.initialize()
                tools = await client.list_tools()
                names = sorted(tool.name for tool in tools.tools)
                assert names == [
                    "act",
                    "play",
                    "process",
                    "record",
                    "screenshot",
                    "system",
                    "window",
                ], names
                status = await client.call_tool("system", {"action": "status"})
                assert not status.isError, status
                assert status.structuredContent is not None
                assert status.structuredContent["data"]["input_policy"] == "explicit_per_call"
                assert status.structuredContent["data"]["default_input"] == "strict_uia"
                assert status.structuredContent["data"]["uia_worker_started"] is False
                windows = await client.call_tool("window", {"action": "list"})
                assert not windows.isError, windows
                refused = await client.call_tool(
                    "act",
                    {
                        "native": False,
                        "target": "foreground",
                        "actions": [{"action": "force_focus"}],
                    },
                )
                assert refused.isError, refused
                assert refused.structuredContent["status"] == "refused"
                final_status = await client.call_tool("system", {"action": "status"})
                assert not final_status.structuredContent["data"]["uia_worker_started"]
                report = {
                    "server": initialized.serverInfo.model_dump(mode="json"),
                    "protocol_version": initialized.protocolVersion,
                    "tools": names,
                    "status": status.model_dump(mode="json"),
                    "window_count": len(windows.structuredContent["data"]),
                    "native_refusal": refused.model_dump(mode="json"),
                    "passed": True,
                }
                output = artifacts / "stdio-smoke.json"
                output.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps({"report": str(output), "tools": names, "passed": True}))


if __name__ == "__main__":
    asyncio.run(main())
