import base64
import io
import json

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ImageContent
from PIL import Image

from minano_computer_use.capture import CaptureFrame
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import create_server


class DummyCapture:
    closed = False

    def screenshot(self, **kwargs):
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), (0, 200, 0)).save(stream, format="PNG")
        return CaptureFrame(stream.getvalue(), "image/png", {"capture_surface": "screen_composite"})

    def close(self):
        self.closed = True


def test_official_memory_client_tools_status_errors_images_and_lifespan():
    async def exercise():
        capture = DummyCapture()
        runtime = Runtime(capture=capture)
        server = create_server(runtime)
        async with create_connected_server_and_client_session(server) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "window",
                "act",
                "screenshot",
                "record",
                "play",
                "process",
                "system",
            }
            status = await client.call_tool("system", {"action": "status"})
            assert not status.isError
            metadata = json.loads(status.content[0].text)
            assert metadata["data"]["default_input"] == "strict_uia"
            denied = await client.call_tool(
                "act",
                {
                    **(
                        {
                            "actions": [{"action": "click", "x": 1, "y": 2}],
                            "target": "fixture",
                            "native": False,
                        }
                    ),
                    "screenshot": False,
                },
            )
            assert denied.isError
            assert json.loads(denied.content[0].text)["status"] == "refused"
            screenshot = await client.call_tool("screenshot", {"format": "png"})
            assert not screenshot.isError
            images = [block for block in screenshot.content if isinstance(block, ImageContent)]
            assert len(images) == 1
            assert base64.b64decode(images[0].data).startswith(b"\x89PNG\r\n\x1a\n")
            assert images[0].mimeType == "image/png"
            assert "image_bytes" not in screenshot.content[0].text
        assert runtime._closed
        assert capture.closed

    anyio.run(exercise)


def test_force_focus_uses_existing_act_tool_and_per_call_native():
    class Native:
        def __init__(self):
            self.calls = []

        def set_cancel_event(self, event):
            pass

        def execute(self, action, target):
            self.calls.append((action, target))
            return {
                "status": "verified",
                "operation": "force_focus",
                "method": "AttachThreadInput+SetForegroundWindow",
                "delivery": "native",
                "focus_changed": True,
                "target_foreground": True,
            }

        def release_all(self):
            return {"status": "ok", "operation": "release_all"}

        def close(self):
            pass

    async def exercise():
        native = Native()
        runtime = Runtime(native=native)
        server = create_server(runtime)
        async with create_connected_server_and_client_session(server) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 7
            act = next(tool for tool in tools if tool.name == "act")
            assert "force_focus" in act.description
            request = {"actions": [{"action": "force_focus"}], "target": "fixture", "native": True}
            denied = await client.call_tool(
                "act", {**({**request, "native": False}), "screenshot": False}
            )
            assert denied.isError
            assert not native.calls
            focused = await client.call_tool("act", {**(request), "screenshot": False})
            assert not focused.isError
            response = json.loads(focused.content[0].text)["data"]["results"][0]["result"]
            assert response["status"] == "verified"
            assert response["delivery"] == "native"
            assert response["method"] == "AttachThreadInput+SetForegroundWindow"
            assert response["focus_changed"] and response["target_foreground"]
            assert len(native.calls) == 1

    anyio.run(exercise)
