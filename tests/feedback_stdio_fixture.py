"""Synthetic read-only MCP fixture; no window lookup, input or desktop capture."""

import json

from minano_computer_use.broker import UIABroker
from minano_computer_use.contracts import result
from minano_computer_use.runtime import Runtime
from minano_computer_use.server import create_server

CREATION_TIME = 134387468188212291


def integer_worker(connection):
    try:
        while True:
            request = json.loads(connection.recv_bytes().decode("utf-8"))
            if request is None:
                return
            creation = request["data"][0]["process_creation_time"]
            assert type(creation) is int and creation == CREATION_TIME
            response = result(
                "ok",
                "list",
                data=request["data"],
                delivery="read_only",
                internal_integer_exact=True,
            )
            connection.send_bytes(json.dumps(response, allow_nan=False).encode("utf-8"))
    finally:
        connection.close()


class SyntheticRuntime(Runtime):
    def window(self, action="list", target=None, selector=None, **kwargs):
        sample = result(
            "ok",
            "list",
            data=[
                {
                    "hwnd": 10,
                    "pid": 20,
                    "process_creation_time": CREATION_TIME,
                    "nested": {"process_creation_time": CREATION_TIME},
                    "unavailable": {"process_creation_time": None},
                }
            ],
            delivery="read_only",
        )
        broker = UIABroker(worker_target=integer_worker)
        try:
            response = broker.call({"action": "list", "data": sample["data"]}, timeout=8)
            assert response["status"] == "ok", response
            creation = response["data"][0]["process_creation_time"]
            assert type(creation) is int and creation == CREATION_TIME
            return response
        finally:
            broker.close()


if __name__ == "__main__":
    create_server(runtime=SyntheticRuntime()).run(transport="stdio")
