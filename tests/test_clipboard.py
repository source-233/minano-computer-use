import threading
import time
from contextlib import contextmanager

import pytest

from minano_computer_use.automation_services import AutomationServices


class Clipboard:
    def __init__(self, text="old\r\n中文 😀"):
        self.text, self.sequence, self.locked = text, 10, False
        self.lock = threading.RLock()
        self.fail_read = False

    @contextmanager
    def clipboard_open(self, cancel_event=None, deadline=None):
        with self.lock:
            assert not self.locked
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

    def clipboard_read(self):
        assert self.locked
        if self.fail_read:
            raise OSError("secret clipboard content")
        return self.text

    def clipboard_write(self, text):
        assert self.locked
        self.text = text
        self.sequence += 1

    def clipboard_sequence(self):
        assert self.locked
        return self.sequence


def test_unicode_exact_readback_without_desktop_mutation():
    adapter = Clipboard()
    services = AutomationServices(adapter=adapter)
    text = "中文\r\n😀\nexact"
    assert services.system("set_clipboard", text)["status"] == "verified"
    assert services.system("get_clipboard")["data"]["text"] == text


@pytest.mark.parametrize("previous", [None, "", "中文\r\n😀"])
def test_session_restores_exact_previous_text(previous):
    adapter = Clipboard(previous)
    services = AutomationServices(adapter=adapter)
    with services.clipboard_session("paste") as token:
        assert adapter.text == "paste"
    assert adapter.text == previous
    assert token.restored and not token.restore_skipped


def test_changed_sequence_is_not_overwritten_even_if_text_matches():
    adapter = Clipboard()
    services = AutomationServices(adapter=adapter)
    with services.clipboard_session("paste") as token:
        with adapter.clipboard_open():
            adapter.clipboard_write("paste")
    assert adapter.text == "paste"
    assert token.restore_skipped and not token.restored


def test_cancelled_operation_still_restores():
    adapter = Clipboard("before")
    event = threading.Event()
    services = AutomationServices(adapter=adapter)
    with (
        pytest.raises(RuntimeError),
        services.clipboard_session(
            "paste", cancel_event=event, deadline=time.monotonic() + 1
        ) as token,
    ):
        event.set()
        raise RuntimeError("fixture")
    assert adapter.text == "before" and token.restored


def test_errors_do_not_leak_backend_clipboard_text():
    adapter = Clipboard()
    adapter.fail_read = True
    outcome = AutomationServices(adapter=adapter).system("get_clipboard")
    assert outcome["status"] == "error"
    assert "secret" not in str(outcome)


def test_restore_failure_is_token_evidence_not_error_content():
    adapter = Clipboard()
    with AutomationServices(adapter=adapter).clipboard_session("paste") as token:
        adapter.fail_read = True
    assert token.restore_error == "CLIPBOARD_RESTORE_FAILED"


def test_partial_write_failure_rolls_back_before_unlock():
    class PartialWrite(Clipboard):
        def clipboard_write(self, text):
            if text == "paste":
                super().clipboard_write(None)
                raise OSError("install failed")
            super().clipboard_write(text)

    adapter = PartialWrite("original")
    with pytest.raises(OSError), AutomationServices(adapter=adapter).clipboard_session("paste"):
        pytest.fail("session must not yield after partial write")
    assert adapter.text == "original"


def test_sequence_compare_and_restore_share_one_atomic_open():
    class RacingClipboard(Clipboard):
        def clipboard_sequence(self):
            assert self.locked
            return super().clipboard_sequence()

        def clipboard_write(self, text):
            assert self.locked
            super().clipboard_write(text)

    adapter = RacingClipboard("before")
    with AutomationServices(adapter=adapter).clipboard_session("paste") as token:
        assert adapter.text == "paste"
    assert token.restored and adapter.text == "before"


@pytest.mark.parametrize("text", [None, "a\0b", 1])
def test_invalid_text_refused_before_write(text):
    adapter = Clipboard()
    assert AutomationServices(adapter=adapter).system("set_clipboard", text)["status"] == "refused"
    assert adapter.sequence == 10
