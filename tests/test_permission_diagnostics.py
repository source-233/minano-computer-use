import json

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_services import Adapter

from minano_computer_use.automation_services import AutomationServices
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import create_server


@pytest.mark.parametrize("startup,call", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("operation", ["close", "kill", "shell", "probe"])
def test_runtime_permission_refusals_identify_the_missing_gate(startup, call, operation):
    services = AutomationServices(startup, startup, adapter=Adapter())
    runtime = Runtime(allow_shell=startup, allow_process_control=startup, services=services)
    try:
        if operation == "close":
            response = runtime.window("close", "Fixture", allow_process_control=call)
        elif operation == "kill":
            response = runtime.process("kill", pid=41001, allow_process_control=call)
        elif operation == "shell":
            response = runtime.process("shell", command="never", allow_shell=call)
        else:
            response = runtime.play(
                script="probe", target="Fixture", native=True, probe="never", allow_shell=call
            )
        assert response["status"] == "refused"
        assert response["error"]["code"] == "PERMISSION_REQUIRED"
        message = response["error"]["message"]
        permission = "shell" if operation in {"shell", "probe"} else "process_control"
        assert ("startup option --allow-" in message) is (not startup)
        assert (f"per-call allow_{permission}=true" in message) is (not call)
        assert not services.adapter.kills and not services.adapter.closes
    finally:
        runtime.close()


@pytest.mark.parametrize("startup,call", [(False, False), (False, True), (True, False)])
def test_service_direct_call_uses_the_same_gate_diagnostics(startup, call):
    services = AutomationServices(allow_process_control=startup, adapter=Adapter())
    response = services.window("close", "Fixture", allow_process_control=call)
    assert response["status"] == "refused"
    assert response["error"]["code"] == "PERMISSION_REQUIRED"
    assert ("startup option" in response["error"]["message"]) is (not startup)
    assert ("per-call" in response["error"]["message"]) is (not call)


@pytest.mark.parametrize("startup,call", [(False, False), (False, True), (True, False)])
def test_mcp_permission_error_keeps_both_transport_representations_and_no_effects(startup, call):
    async def exercise():
        services = AutomationServices(startup, startup, adapter=Adapter())
        runtime = Runtime(allow_shell=startup, allow_process_control=startup, services=services)
        async with create_connected_server_and_client_session(create_server(runtime)) as client:
            for tool, payload in (
                ("window", {"action": "close", "target": "Fixture", "allow_process_control": call}),
                ("process", {"action": "kill", "pid": 41001, "allow_process_control": call}),
                ("process", {"action": "shell", "command": "never", "allow_shell": call}),
            ):
                result = await client.call_tool(tool, payload)
                assert result.isError
                response = result.structuredContent
                assert json.loads(result.content[0].text) == response
                assert response["status"] == "refused"
                assert response["error"]["code"] == "PERMISSION_REQUIRED"
                assert not response.get("may_have_acted", False)
            assert not services.adapter.kills and not services.adapter.closes

    anyio.run(exercise)
