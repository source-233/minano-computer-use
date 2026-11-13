"""Real MCP transport plus JavaScript round-trip of synthetic identity facts."""

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = "134387468188212291"
JS = """
const fs = require('fs');
const envelope = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const text = JSON.parse(envelope.text);
const structured = envelope.structured;
function check(value) {
  if (value.internal_integer_exact !== true) {
    throw new Error('Worker did not confirm exact internal Python identity');
  }
  const item = value.data[0];
  if (item.process_creation_time !== '134387468188212291' ||
      item.nested.process_creation_time !== '134387468188212291' ||
      item.unavailable.process_creation_time !== null ||
      item.hwnd !== 10 || item.pid !== 20) {
    throw new Error('Public identity fields failed exact JavaScript comparison');
  }
  const again = JSON.parse(JSON.stringify(value));
  if (again.data[0].process_creation_time !== item.process_creation_time) {
    throw new Error('Identity timestamp did not survive JSON round-trip');
  }
}
check(text);
check(structured);
if (JSON.stringify(text) !== JSON.stringify(structured)) {
  throw new Error('MCP text and structured results differ');
}
console.log(JSON.stringify({internal_python_ipc_exact:true, exact_decimal_string:true, nested:true,
  unknown_is_null:true, hwnd_pid_remain_numbers:true, representations_match:true}));
"""


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    artifacts = root / ".artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    javascript = shutil.which("bun") or shutil.which("node")
    if javascript is None:
        raise SystemExit("A JavaScript runtime is required for this verification")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(root / "tests/feedback_stdio_fixture.py")],
        cwd=str(root),
    )
    with (artifacts / "feedback-server.stderr.log").open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=15)
            ) as client:
                await client.initialize()
                response = await client.call_tool(
                    "window", {"action": "list", "max_output_chars": 2048}
                )
                assert not response.isError, response
                texts = [part.text for part in response.content if part.type == "text"]
                assert len(texts) == 1, texts
                text_result = json.loads(texts[0])
                structured = response.structuredContent
                assert text_result == structured
                assert structured["data"][0]["process_creation_time"] == EXPECTED
                envelope = {"text": texts[0], "structured": structured}
    wire = artifacts / "feedback-wire.json"
    wire.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    process = subprocess.run(
        [javascript, "-e", JS, str(wire)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    checks = json.loads(process.stdout)
    assert all(value is True for value in checks.values()), checks
    report = {
        "passed": True,
        "checks": checks,
        "javascript_runtime": Path(javascript).name,
        "synthetic_identity": True,
        "native_input_used": False,
        "desktop_read": False,
        "boundary": "Transport/data fidelity only; no application consumption claim",
    }
    output = artifacts / "feedback-smoke.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **report}))


if __name__ == "__main__":
    asyncio.run(main())
