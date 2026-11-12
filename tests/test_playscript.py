import pytest

from minano_computer_use.playscript import evaluate_condition, parse_script


def test_full_script_and_dynamic_markers():
    plan = parse_script("""# only whole-line comments
hold ctrl+a 20ms
tap enter 2
down shift+w
up shift+w
look -5 2
move -10 40
lmb
rmb 10 20
mmb
scroll 2 horizontal
type hello world; # text not commands
paste "a;b # c"
wait 0.1s
probe
until sample['done'] and score >= 2
shot
""")
    assert len(plan) == 17
    assert plan[0]["seconds"] == 0.02
    assert plan[1] == plan[2]
    assert plan[3]["key"] == [16, 87]
    assert plan[8]["button"] == "right"
    assert plan[11]["text"] == "hello world; # text not commands"
    assert plan[12]["text"] == "a;b # c"
    assert plan[-3] == {"action": "probe"}
    assert plan[-2] == {"action": "until", "condition": "sample['done'] and score >= 2"}
    assert plan[-1] == {"action": "shot"}


@pytest.mark.parametrize(
    "script",
    [
        "tap A\nunknown x",
        "tap A; shot",
        "wait -1",
        "wait nan",
        "hold A 11",
        "tap A 0",
        "tap A 101",
        "tap A 1.5",
        "tap A 99\ntap B 2",
        "shot x",
        "probe x",
        "until f()",
        "until x.y",
        "until __import__('os')",
        "until x ** 100",
        "until [x for x in y]",
        "until (lambda: 1)()",
        "move 1.2 3",
        "paste",
        'type "unterminated',
        "tap A\n" * 101,
        "type " + "a" * 65536,
    ],
)
def test_whole_plan_validation(script):
    with pytest.raises(ValueError):
        parse_script(script)


def test_text_quotes_and_tabs_and_literal_flag():
    assert parse_script("type\thello there")[0]["text"] == "hello there"
    assert parse_script('type --literal "a;b"')[0]["literal"] is True
    assert parse_script("type abc # def")[0]["text"] == "abc # def"


def test_safe_condition():
    sample = {"nested": {"x": 3}, "ready": True, "score": 5}
    assert evaluate_condition("ready and nested['x'] * 2 + score >= 11", sample)
    assert not evaluate_condition("not ready or score < 2", sample)
    assert evaluate_condition("False and missing", sample) is False
    assert evaluate_condition("sample['score'] // 2 == 2", sample)


@pytest.mark.parametrize(
    "expr,sample",
    [
        ("x()", {}),
        ("x.y", {}),
        ("__builtins__", {}),
        ("x ** 2", {"x": 1}),
        ("'a' * 10", {}),
        ("10 / 0", {}),
        ("x + 1", {"x": 10**15}),
        ("x", {"x": object()}),
        ("missing", {}),
        ("x[0]", {"x": [1]}),
        ("x['missing']", {"x": {}}),
        ("1e999", {}),
        ("999999999999999999999999", {}),
        ("not " * 30 + "True", {}),
        (" + ".join(["1"] * 100), {}),
        (repr("x" * 4097), {}),
    ],
)
def test_condition_refuses_unbounded_or_executable_constructs(expr, sample):
    with pytest.raises(ValueError):
        evaluate_condition(expr, sample)


@pytest.mark.parametrize(
    "direction,amount,horizontal",
    [
        ("up", 240, False),
        ("down", -240, False),
        ("left", -240, True),
        ("right", 240, True),
    ],
)
def test_exact_old_direction_first_scroll(direction, amount, horizontal):
    expected = {"action": "wheel", "amount": amount, "horizontal": horizontal}
    assert parse_script(f"scroll {direction} 2") == [expected]
    assert parse_script(f"scroll 2 {direction}") == [expected]


@pytest.mark.parametrize("script", ["scroll up", "scroll up left", "scroll 2 3"])
def test_ambiguous_scroll_syntax_refused(script):
    with pytest.raises(ValueError):
        parse_script(script)
