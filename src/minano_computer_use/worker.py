"""The UIA apartment lives exclusively on this spawned process's main thread."""

import contextlib
import json
import sys

from .contracts import READ_ACTIONS, result
from .output import bound_result


def worker_main(connection):
    # Third-party imports, COM diagnostics and close paths may print to stdout.
    with contextlib.redirect_stdout(sys.stderr):
        _serve(connection)


def _serve(connection):
    backend = None
    try:
        while True:
            request = json.loads(connection.recv_bytes().decode("utf-8"))
            if request is None:
                break
            if not isinstance(request, dict):
                break
            try:
                if backend is None:
                    from .uia_backend import UIABackend

                    backend = UIABackend()
                response = backend.execute(request)
            except Exception as exc:
                may_have_acted = request.get("action") not in READ_ACTIONS
                response = result(
                    "unknown" if may_have_acted else "error",
                    str(request.get("action", "uia")),
                    code="WORKER_ERROR",
                    message=str(exc),
                    may_have_acted=may_have_acted,
                )
            response = bound_result(
                response,
                request.get("max_output_chars", 24000),
                request.get("max_output_bytes", 65536),
            )
            connection.send_bytes(json.dumps(response, allow_nan=False).encode("utf-8"))
    except (EOFError, OSError, ValueError, TypeError):
        pass
    finally:
        try:
            if backend is not None:
                backend.close()
        finally:
            connection.close()
