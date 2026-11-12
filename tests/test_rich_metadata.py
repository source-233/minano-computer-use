from types import SimpleNamespace as NS

import pytest
from test_uia_backend import Control, backend, run


def test_rich_metadata_is_opt_in_and_fields_are_bounded():
    control = Control("item")
    control.ClassName = "C" * 5000
    control.LocalizedControlType = "button"
    control.BoundingRectangle = NS(left=1, top=2, right=3, bottom=4)
    response = run(control)
    basic = response["data"]
    assert "class_name" not in basic
    rich = run(control, rich=True, text_limit=12)["data"]
    assert rich["class_name"] == "C" * 4096
    assert rich["frame"]["coordinate_space"] == "provider_screen"
    assert rich["actions"] == []


def test_rich_per_property_failure_does_not_fail_inspect_or_claim_false():
    class Broken(Control):
        def __getattribute__(self, name):
            if name == "IsEnabled":
                raise RuntimeError("provider read failed")
            return super().__getattribute__(name)

    response = run(Broken("item"), rich=True)
    assert response["status"] == "ok"
    assert response["data"]["enabled"] is None
    assert response["data"]["unavailable"]["enabled"]["status"] == "unavailable"
    assert "provider read failed" not in str(response)


def test_actions_only_advertise_retrievable_supported_patterns():
    class Value:
        IsReadOnly = True
        Value = "ignored"

        def SetValue(self, value, waitTime=0):
            raise AssertionError("metadata must not invoke patterns")

    class Invoke:
        def Invoke(self, waitTime=0):
            raise AssertionError("metadata must not invoke patterns")

    control = Control("item", patterns={1: Invoke(), 2: Value()})
    rich = run(control, rich=True)["data"]
    assert rich["actions"] == ["invoke"]


def test_password_value_is_redacted_for_inspect_text_and_mutation():
    class Value:
        IsReadOnly = False
        Value = "PRIVATE_OLD_VALUE"

        def SetValue(self, value, waitTime=0):
            self.Value = value
            return True

    control = Control("secret", patterns={2: Value()})
    control.IsPassword = True
    inspect = run(control, rich=True)["data"]
    text = run(control, "text")["data"]["nodes"][0]
    mutation = run(control, "set_value", value="SENSITIVE_INPUT")["data"]
    assert inspect["value_redacted"] is True
    assert "value" not in inspect
    assert text["text_redacted"] is True
    assert "text" not in text
    assert "SENSITIVE_INPUT" not in str(mutation)
    assert "PRIVATE_OLD_VALUE" not in str(mutation)


@pytest.mark.parametrize("action", ["inspect", "tree", "text", "find"])
def test_unknown_password_never_reads_value_or_document(action):
    class Value:
        IsReadOnly = False

        @property
        def Value(self):
            raise AssertionError("unknown-password values must not be read")

        def SetValue(self, value, waitTime=0):
            raise AssertionError("discovery must not mutate")

    control = Control("item", patterns={2: Value()})
    del control.IsPassword
    response = run(control, action, rich=True, **({"query": "item"} if action == "find" else {}))
    assert response["status"] == "ok"
    assert "AssertionError" not in str(response)


def test_password_provider_exception_does_not_echo_entered_value():
    class Value:
        IsReadOnly = False
        Value = "PRIVATE_OLD"

        def SetValue(self, value, waitTime=0):
            raise RuntimeError("Rejected private input " + value)

    control = Control("item", patterns={2: Value()})
    control.IsPassword = True
    response = run(control, "set_value", value="PASSWORD_ENTERED")
    assert response["status"] == "unknown"
    assert "PASSWORD_ENTERED" not in str(response)
    assert "PRIVATE_OLD" not in str(response)


def test_advertised_pattern_failure_is_local_unavailable_evidence():
    control = Control("item")
    service = backend(control)
    service.auto.PropertyId = NS(IsInvokePatternAvailableProperty=42)
    control.GetPropertyValue = lambda property_id: True
    response = service.execute({"action": "inspect", "target": "App", "rich": True})
    service.close()
    assert response["status"] == "ok"
    assert response["data"]["patterns"]["Invoke"]["status"] == "unavailable"
    assert "invoke" not in response["data"]["actions"]


def test_basic_tree_does_not_fetch_rich_properties_or_patterns():
    class Basic(Control):
        def __getattribute__(self, name):
            if name in {"IsEnabled", "BoundingRectangle", "GetPattern", "IsPassword"}:
                raise AssertionError("basic tree must avoid rich provider reads")
            return super().__getattribute__(name)

    assert run(Basic("root"), "tree")["status"] == "ok"


def test_ancestor_text_does_not_leak_password_descendant():
    root = Control(
        "root",
        children=[Control("secret")],
        patterns={8: NS(DocumentRange=NS(GetText=lambda limit: "PASSWORD_BODY"))},
    )
    root.children[0].IsPassword = True
    response = run(root, "text", rich=True)
    assert "PASSWORD_BODY" not in str(response)
    assert response["data"]["nodes"][0]["text_redacted"] is True


def test_rich_discovery_preserves_token_binding_across_volatile_changes():
    control = Control("item")
    service = backend(control)
    response = service.execute({"action": "find", "target": "App", "query": "item", "rich": True})
    token = response["data"]["matches"][0]["token"]
    control.HasKeyboardFocus = True
    control.IsOffscreen = True
    response = service.execute({"action": "inspect", "target": "App", "token": token})
    service.close()
    assert response["status"] == "ok"


@pytest.mark.parametrize("action", ["tree", "text", "inspect", "find"])
def test_known_password_discovery_never_reads_plaintext(action):
    class Value:
        IsReadOnly = False

        @property
        def Value(self):
            raise AssertionError("password read forbidden")

        def SetValue(self, value, waitTime=0):
            raise AssertionError("discovery write forbidden")

    class Text:
        DocumentRange = NS(GetText=lambda n: (_ for _ in ()).throw(AssertionError("secret")))

    control = Control("item", patterns={2: Value(), 8: Text()})
    control.IsPassword = True
    response = run(control, action, rich=True, **({"query": "item"} if action == "find" else {}))
    assert response["status"] == "ok"
    assert "AssertionError" not in str(response)


def test_unknown_password_mutation_can_verify_privately_without_readback_echo():
    class Value:
        IsReadOnly = False
        Value = "PRIVATE_OLD"

        def SetValue(self, value, waitTime=0):
            self.Value = value
            return True

    control = Control("item", patterns={2: Value()})
    del control.IsPassword
    response = run(control, "set_value", value="PRIVATE_INPUT")
    assert response["status"] == "verified"
    assert response["data"]["value_redacted"] is True
    assert "PRIVATE_INPUT" not in str(response)
    assert "PRIVATE_OLD" not in str(response)


def test_rich_metadata_respects_whole_output_budget():
    import json

    control = Control("root", children=[Control("x" * 4000) for _ in range(12)])
    response = run(control, "tree", rich=True, max_output_chars=1800, max_output_bytes=2500)
    encoded = json.dumps(response, ensure_ascii=False)
    assert len(encoded) <= 1800
    assert len(encoded.encode("utf-8")) <= 2500


def test_mutating_dynamic_password_flag_does_not_reveal_original_or_entered_value():
    class Value:
        IsReadOnly = False
        Value = "PRIVATE_OLD"

        def SetValue(self, value, waitTime=0):
            self.Value = value
            control.IsPassword = False
            return True

    control = Control("item", patterns={2: Value()})
    control.IsPassword = True
    response = run(control, "set_value", value="PRIVATE_INPUT")
    assert response["status"] == "verified"
    assert "PRIVATE" not in str(response)


@pytest.mark.parametrize("password", [True, None])
@pytest.mark.parametrize("matched", [True, False])
def test_wait_value_password_and_unknown_redact_last_observations(password, matched):
    control = Control("item", patterns={2: NS(Value="PRIVATE_OBSERVED")})
    if password is None:
        del control.IsPassword
    else:
        control.IsPassword = password
    response = run(
        control,
        "wait_for",
        selector="item",
        timeout=0.01,
        poll_interval=0.001,
        condition={"value": "PRIVATE_OBSERVED" if matched else "PRIVATE_EXPECTED"},
    )
    assert response["status"] == ("verified" if matched else "error")
    assert response["last_observation"]["value"] is None
    assert response["data"]["last_observation"]["value_redacted"] is True
    assert "PRIVATE" not in str(response)


@pytest.mark.parametrize("password", [True, None])
def test_wait_value_provider_error_never_echoes_secret_or_expected(password):
    class Value:
        @property
        def Value(self):
            raise RuntimeError("PRIVATE_PROVIDER_SECRET")

    control = Control("item", patterns={2: Value()})
    if password is None:
        del control.IsPassword
    else:
        control.IsPassword = password
    response = run(
        control,
        "wait_for",
        selector="item",
        timeout=0.01,
        condition={"value": "PRIVATE_EXPECTED"},
    )
    assert response["status"] == "error"
    assert response["error"]["code"] == "UIA_ERROR"
    assert "PRIVATE" not in str(response)


@pytest.mark.parametrize("action", ["tree", "text", "find", "state", "inspect"])
@pytest.mark.parametrize("unsafe", ["password", "unknown", "truncated"])
def test_aggregate_value_is_not_read_when_descendant_safety_unproven(action, unsafe):
    reads = []

    class AggregateValue:
        IsReadOnly = True

        @property
        def Value(self):
            reads.append(True)
            raise AssertionError("DESCENDANT_PRIVATE_VALUE must not be read")

    child = Control("child")
    if unsafe == "password":
        child.IsPassword = True
    elif unsafe == "unknown":
        del child.IsPassword
    root = Control("root", children=[child], patterns={2: AggregateValue()})
    kwargs = {"rich": True}
    if action == "find":
        kwargs["query"] = "root"
    if action == "state":
        kwargs["target_binding"] = {"hwnd": 10, "pid": 20, "process_creation_time": 1234}
    if unsafe == "truncated":
        kwargs["depth"] = 0
    response = run(root, action, **kwargs)
    assert response["status"] == "ok"
    assert reads == []
    assert "DESCENDANT_PRIVATE_VALUE" not in str(response)
    if action in {"tree", "text", "state"}:
        entry = response["data"]["nodes"][0]
    elif action == "find":
        entry = response["data"]["matches"][0]
    else:
        entry = response["data"]
        assert entry["patterns"]["Value"]["value_redacted"] is True
    assert entry["value_redacted"] is True
    assert "value" not in entry


@pytest.mark.parametrize("action", ["tree", "text", "find", "state", "inspect"])
def test_known_safe_leaf_value_is_preserved_beside_password_sibling(action):
    leaf = Control("leaf", patterns={2: NS(Value="public", IsReadOnly=True)})
    secret = Control("secret")
    secret.IsPassword = True
    root = Control("root", children=[secret, leaf])
    kwargs = {"rich": True}
    if action == "find":
        kwargs["query"] = "leaf"
    if action == "inspect":
        kwargs["selector"] = "leaf"
    if action == "state":
        kwargs["target_binding"] = {"hwnd": 10, "pid": 20, "process_creation_time": 1234}
    response = run(root, action, **kwargs)
    assert response["status"] == "ok"
    if action in {"tree", "text", "state"}:
        entry = response["data"]["nodes"][2]
    elif action == "find":
        entry = response["data"]["matches"][0]
    else:
        entry = response["data"]
        assert entry["patterns"]["Value"]["Value"] == "public"
    assert entry["value"] == "public"


def test_aggregate_mutation_and_wait_readbacks_remain_private():
    class AggregateValue:
        IsReadOnly = False
        Value = "DESCENDANT_PRIVATE_VALUE"

        def SetValue(self, value, waitTime=0):
            self.Value = value
            return True

    secret = Control("secret")
    secret.IsPassword = True
    root = Control("root", children=[secret], patterns={2: AggregateValue()})
    wait = run(root, "wait_for", selector="root", condition={"value": "DESCENDANT_PRIVATE_VALUE"})
    mutation = run(root, "set_value", value="PRIVATE_ENTERED")
    assert wait["status"] == "verified"
    assert mutation["status"] == "verified"
    assert wait["last_observation"]["value_redacted"] is True
    assert mutation["data"]["value_redacted"] is True
    assert "PRIVATE" not in str(wait)
    assert "PRIVATE" not in str(mutation)
