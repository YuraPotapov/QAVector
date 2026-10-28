"""Variables in a launch address: syntax, types, the four places, and problems."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import launchvars as lv


def _defs(**specs):
    found, problems = lv.definitions_from(specs)
    assert problems == []
    return found


PORT = {"port": {"type": "port", "default": 8069}}


def _resolve(template, layers=(), **specs):
    return lv.resolve(template, _defs(**(specs or PORT)), list(layers))


# ------------------------------------------------------------------ syntax

def test_the_short_and_the_long_spelling_name_the_same_variable():
    for template in ("localhost:{{port}}", "localhost:{{vars.port}}",
                     "localhost:{{ port }}", "localhost:{{  vars.port  }}"):
        result = _resolve(template)
        assert result.problems == [] and result.text == "localhost:8069", template


def test_the_parts_say_which_text_a_variable_put_there():
    result = _resolve("http://{{host}}:{{port}}/web/login",
                      host={"type": "string", "default": "localhost"},
                      port={"type": "port", "default": 8069})
    assert result.text == "http://localhost:8069/web/login"
    assert result.parts == [("http://", ""), ("localhost", "host"), (":", ""),
                            ("8069", "port"), ("/web/login", "")]


def test_references_are_listed_once_in_order():
    assert lv.references("{{b}}x{{a}}{{vars.b}}") == ["b", "a"]


@pytest.mark.parametrize("template, code", [
    ("localhost:{{port", "unterminated"),
    ("localhost:port}}", "unmatched"),
    ("localhost:{{}}", "empty"),
    ("localhost:{{port + 1}}", "expression"),
    ("localhost:{{port()}}", "expression"),
    ("localhost:{{env.origin}}", "reserved"),
    ("localhost:{{user}}", "reserved"),
    ("localhost:{{other.port}}", "expression"),
    ("localhost:{{nobody}}", "unknown"),
])
def test_a_template_that_cannot_be_filled_says_why(template, code):
    result = _resolve(template)
    assert [p.code for p in result.problems] == [code]
    assert result.problems[0].field == "address"


def test_substitution_happens_once():
    """A value holding {{...}} goes in as it is - no hidden second pass."""
    result = _resolve("x/{{path}}", [lv.Layer("this run", {"path": "{{port}}"})],
                      path={"type": "string"}, port={"type": "port", "default": 1})
    assert result.problems == [] and result.text == "x/{{port}}"


def test_names_are_case_sensitive_and_reserved_words_cannot_be_defined():
    assert _resolve("{{Port}}").problems[0].code == "unknown"
    _found, problems = lv.definitions_from({"env": {"type": "string"},
                                            "1st": {"type": "string"},
                                            "ok_name": {"type": "string"}})
    assert sorted(p.field for p in problems) == ["1st", "env"]


# ------------------------------------------------------------------ the four places

def _layers(environment=None, configuration=None, run=None):
    return [lv.Layer("environment", environment or {}),
            lv.Layer("configuration", configuration or {}),
            lv.Layer("this run", run or {})]


def test_the_nearest_place_wins_and_names_what_it_overrode():
    result = _resolve("localhost:{{port}}", _layers({"port": 8070}, {"port": 8071},
                                                    {"port": 8074}))
    assert result.text == "localhost:8074"
    value = result.values["port"]
    assert (value.value, value.source) == (8074, "this run")
    assert (value.inherited, value.inherited_source) == ("8071", "configuration")


@pytest.mark.parametrize("layers, expected, source", [
    (_layers(), "8069", "project"),
    (_layers({"port": 8070}), "8070", "environment"),
    (_layers({"port": 8070}, {"port": 8071}), "8071", "configuration"),
    (_layers({"port": 8070}, None, {"port": "8075"}), "8075", "this run"),
])
def test_each_place_in_turn(layers, expected, source):
    result = _resolve("{{port}}", layers)
    assert result.text == expected and result.values["port"].source == source


def test_nothing_said_inherits_and_none_is_nothing_said():
    result = _resolve("{{port}}", _layers({"port": 8070}, {"port": None}))
    assert result.values["port"].source == "environment"


def test_an_empty_string_is_a_value_for_a_string_only():
    text = _resolve("x{{suffix}}", _layers(None, None, {"suffix": ""}),
                    suffix={"type": "string", "default": "/web"})
    assert text.problems == [] and text.text == "x"
    port = _resolve("{{port}}", _layers(None, None, {"port": ""}))
    assert [p.code for p in port.problems] == ["type"]


def test_a_variable_with_no_value_anywhere_is_missing():
    result = _resolve("{{port}}", port={"type": "port"})
    assert [(p.field, p.code) for p in result.problems] == [("port", "missing")]


# ------------------------------------------------------------------ types

@pytest.mark.parametrize("raw", [0, 65536, -1, "abc", "80.5", True])
def test_a_port_is_a_whole_number_from_1_to_65535(raw):
    result = _resolve("{{port}}", _layers(None, None, {"port": raw}))
    assert [(p.field, p.code) for p in result.problems] == [("port", "type")]


@pytest.mark.parametrize("raw", [1, 65535, "8069", " 8070 "])
def test_ports_that_are_fine(raw):
    assert _resolve("{{port}}", _layers(None, None, {"port": raw})).problems == []


def test_integers_booleans_and_choices():
    defs = dict(n={"type": "integer", "default": -3},
                debug={"type": "boolean", "default": "yes"},
                mode={"type": "string", "default": "a", "choices": ["a", "b"]})
    result = _resolve("{{n}}/{{debug}}/{{mode}}", **defs)
    assert result.text == "-3/true/a"
    wrong = _resolve("{{mode}}", _layers(None, None, {"mode": "c"}), **defs)
    assert [p.code for p in wrong.problems] == ["type"]


def test_a_default_of_the_wrong_type_is_a_problem_of_the_definition():
    _found, problems = lv.definitions_from({"port": {"type": "port", "default": "x"}})
    assert [(p.field, p.code) for p in problems] == [("port", "type")]
    _found, problems = lv.definitions_from({"p": {"type": "colour"}})
    assert [p.code for p in problems] == ["type"]
