"""Published MCP definitions are a usable contract, not only Python annotations."""

import json

import anyio
import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp.shared.memory import create_connected_server_and_client_session

from minano_computer_use.contracts import NATIVE_ACTIONS, UIA_ACTIONS
from minano_computer_use.input_plan import normalize_native_action
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import create_server
from minano_computer_use.tool_schema import NATIVE_ALIASES


@pytest.fixture(scope="module")
def tools():
    async def observe():
        runtime = Runtime()
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            result = (await client.list_tools()).tools
        assert runtime._closed
        return {item.name: item for item in result}

    return anyio.run(observe)


def validator(tools, name):
    return Draft202012Validator(tools[name].inputSchema)


def action_properties(tools, name):
    actions = tools[name].inputSchema["properties"]["actions"]
    array = actions if name == "act" else actions["anyOf"][0]
    return array["items"]["properties"]


def test_all_public_parameters_have_descriptions_and_defaults_validate(tools):
    assert set(tools) == {"window", "act", "play", "screenshot", "record", "process", "system"}
    total = 0
    for name, tool in tools.items():
        schema = tool.inputSchema
        Draft202012Validator.check_schema(schema)
        assert (
            not {"ctx", "scope", "_request_cancel", "target_binding"} & schema["properties"].keys()
        )
        for field, definition in schema["properties"].items():
            assert len(definition.get("description", "")) >= 15, (name, field)
            total += 1
            if "default" in definition:
                Draft202012Validator(definition).validate(definition["default"])
        assert len(tool.description) < 2200, name  # Explain, don't paste the full README.
        assert "Returns" in tool.description, name
    assert total == 122


def test_tool_routing_and_safety_descriptions_are_not_lost(tools):
    for name in ("window", "act", "play", "process"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is True
    for name in ("screenshot", "record"):
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    assert tools["system"].annotations.readOnlyHint is False  # Clipboard/cancel mutate.
    assert "set_clipboard" in tools["system"].inputSchema["properties"]["action"]["enum"]
    assert "kill" in tools["process"].inputSchema["properties"]["action"]["enum"]
    assert "--allow-shell" in tools["process"].description
    assert "--allow-process-control" in tools["process"].description
    assert "not process-exit evidence" in tools["process"].description
    assert "NOT mean input was released" in tools["system"].description
    assert "never auto-refocus" in tools["play"].description
    assert "act" in tools["play"].description and "play" in tools["act"].description
    assert "record" in tools["screenshot"].description


def test_published_action_objects_explain_supported_fields_and_timing(tools):
    for name in ("act", "play"):
        properties = action_properties(tools, name)
        assert UIA_ACTIONS | NATIVE_ACTIONS <= set(properties["action"]["enum"])
        for field in ("selector", "token", "key", "seconds", "recipient", "to", "path"):
            assert properties[field]["description"], (name, field)
        assert properties["selector"]["anyOf"][1]["additionalProperties"] is False
        assert properties["condition"]["anyOf"][0]["required"] == ["exists"]
    assert "at" not in action_properties(tools, "act")
    at = action_properties(tools, "play")["at"]
    assert at["minimum"] == 0 and at["maximum"] == 60
    assert "outer seconds" in at["description"]
    seconds = tools["play"].inputSchema["properties"]["seconds"]
    assert seconds["minimum"] == 0.1 and seconds["maximum"] == 30
    assert "Exactly one" in tools["play"].inputSchema["properties"]["script"]["description"]


@pytest.mark.parametrize(
    "name,payload",
    [
        ("window", {"action": "state", "target": "hwnd:0x123", "include_tokens": True}),
        (
            "window",
            {
                "action": "wait_for",
                "target": "App",
                "selector": {"automation_id": "done"},
                "condition": {"exists": True},
            },
        ),
        (
            "window",
            {"action": "wait_for", "target": "App", "token": "opaque", "condition": {"value": ""}},
        ),
        (
            "act",
            {
                "actions": [
                    {"action": "invoke", "selector": {"control_type": "Button", "name": "OK"}}
                ]
            },
        ),
        ("act", {"actions": [{"action": "text"}], "target": "App"}),  # UIA root text read.
        ("act", {"actions": [{"action": "list", "target": None}]}),
        ("act", {"native": True, "actions": [{"action": "click", "modifiers": []}]}),
        (
            "act",
            {
                "actions": [{"type": "hold_key", "keys": ["Ctrl", "C"], "duration": 0.1}],
                "native": True,
            },
        ),
        (
            "act",
            {
                "actions": [{"action": "drag", "x": 10, "y": 20, "to": [50, 60], "seconds": 0.5}],
                "native": True,
            },
        ),
        (
            "act",
            {
                "actions": [{"action": "set_value", "recipient": "hwnd:0x124", "value": ""}],
                "input_mode": "message",
            },
        ),
        ("act", {"actions": [{"action": "invoke", "selector": None, "token": "opaque"}]}),
        ("act", {"actions": [{"action": "invoke", "selector": "OK", "token": None}]}),
        (
            "act",
            {
                "native": True,
                "actions": [{"action": "drag", "x": 1, "y": 2, "path": [{"coordinate": [3, 4]}]}],
            },
        ),
        (
            "play",
            {
                "target": "App",
                "native": True,
                "seconds": 2,
                "actions": [{"at": 0.2, "action": "hold", "key": "W", "seconds": 0.2}],
            },
        ),
        (
            "play",
            {
                "target": "App",
                "native": True,
                "seconds": 2,
                "script": "hold W 0.2\nlook 40 0\nshot",
            },
        ),
        (
            "screenshot",
            {"region": {"left": 0, "top": 0, "width": 10, "height": 20}, "region_space": "screen"},
        ),
        ("record", {"seconds": 60, "fps": 1, "keep_frames": True}),
        ("process", {"action": "launch", "argv": ["notepad.exe"]}),
        ("system", {"action": "set_clipboard", "text": ""}),
    ],
)
def test_canonical_and_legacy_examples_validate_against_published_schema(tools, name, payload):
    validator(tools, name).validate(payload)
    if name in {"act", "play"} and payload.get("actions"):
        runtime = Runtime()
        try:
            mode = runtime._input_mode(
                payload.get("native", False), payload.get("input_mode"), "scancode"
            )
            runtime._validate_actions(
                payload["actions"],
                payload.get("target", "App"),
                mode == "native",
                True,
                5,
                timed=name == "play",
                input_mode=mode,
            )
        finally:
            runtime.close()


@pytest.mark.parametrize(
    "name,payload",
    [
        ("play", {"seconds": 31}),
        ("play", {"actions": [{"at": 62, "action": "hold", "key": "W"}]}),
        ("act", {"actions": [{"at": 0.2, "action": "hold", "key": "W"}]}),
        ("act", {"actions": [{"action": "hold"}]}),
        ("act", {"actions": [{"action": "set_value"}]}),
        ("act", {"actions": [{"action": "invoke", "selector": "OK", "token": "opaque"}]}),
        ("act", {"actions": [{"action": "invoke", "type": "click"}]}),
        ("act", {"actions": [{"action": "click", "_coordinate_binding": {}}]}),
        ("act", {"actions": [{"action": "drag", "x": 1, "y": 2}]}),
        ("act", {"actions": [{"action": "drag", "x": 1, "y": 2, "to": [3, 4], "path": [[3, 4]]}]}),
        ("window", {"selector": {"name": "OK", "unsafe": True}}),
        ("window", {"condition": {"exists": True, "value": ""}}),
        ("window", {"condition": {"toggle_state": 4}}),
    ],
)
def test_schema_exposes_obvious_invalid_shapes_before_trial_and_error(tools, name, payload):
    with pytest.raises(ValidationError):
        validator(tools, name).validate(payload)


def test_published_examples_are_self_consistent_and_size_is_bounded(tools):
    encoded = json.dumps([tool.model_dump(mode="json") for tool in tools.values()])
    assert len(encoded) < 90000  # No giant repeated per-action union / README in tools/list.
    for tool in tools.values():
        for definition in tool.inputSchema["properties"].values():
            for example in definition.get("examples", []):
                Draft202012Validator(definition).validate(example)
    for name in ("act", "play"):
        for condition in ("exists", "value", "selected", "toggle_state", "expand_state"):
            assert any(
                variant["required"] == [condition]
                for variant in action_properties(tools, name)["condition"]["anyOf"]
            )


@pytest.mark.parametrize("alias", sorted(NATIVE_ALIASES))
def test_all_documented_native_aliases_are_still_valid(tools, alias):
    if alias == "hold_key":
        fields = {"keys": "Ctrl+A", "duration": 0.1}
    elif alias == "type":
        fields = {"value": "hello"}
    elif alias == "scroll":
        fields = {"scroll_amount": 2, "scroll_direction": "down"}
    elif alias == "mouse_move_relative":
        fields = {"x": -10, "y": 5}
    else:
        fields = {"coordinate": [10, 20]}
    payload = {"type": alias, **fields}
    assert normalize_native_action(payload)["action"] in NATIVE_ACTIONS
    validator(tools, "act").validate({"native": True, "actions": [payload]})


def test_schema_metadata_does_not_coerce_drop_or_default_action_fields():
    async def exercise():
        runtime = Runtime()
        server = create_server(runtime)
        tools = {item.name: item for item in server._tool_manager.list_tools()}
        # This asserts SDK validation preserves malformed/legacy dictionaries so
        # Runtime, not an unrelated schema model, owns whole-plan refusals.
        payload = {"actions": [{"type": "hold_key", "keys": "W", "duration": 0.2, "bad": 7}]}
        model = tools["act"].fn_metadata.arg_model.model_validate(payload)
        assert model.model_dump_one_level()["actions"] == payload["actions"]
        runtime.close()

    anyio.run(exercise)


def test_invalid_play_and_batch_still_refuse_before_input_or_capture():
    class Never:
        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            pytest.fail(f"Invalid plan accessed an effect backend: {name}")

    async def exercise():
        runtime = Runtime(native=Never(), capture=Never())
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            for payload in (
                {"actions": [{"at": 62, "action": "hold", "key": "W"}]},
                {"seconds": 2, "actions": [{"at": 3, "action": "hold", "key": "W"}]},
                {"actions": [{"action": "key", "key": "W"}], "script": "tap W"},
            ):
                response = await client.call_tool(
                    "play", {"native": True, "target": "fixture", **payload}
                )
                assert response.isError
                result = json.loads(response.content[0].text)
                assert result == response.structuredContent and result["status"] == "refused"
            response = await client.call_tool(
                "act",
                {
                    "native": True,
                    "target": "fixture",
                    "screenshot": False,
                    "actions": [
                        {"action": "key", "key": "W"},
                        {"action": "hold", "key": "invalid-key"},
                    ],
                },
            )
            assert response.isError
            assert response.structuredContent["status"] == "refused"

    anyio.run(exercise)
