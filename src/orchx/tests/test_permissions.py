"""Тесты для :mod:`orchx.agent.permissions`."""

from __future__ import annotations

from orchx.agent.permissions import Permissions, parse_permissions


def test_default_permissions_match_kilo_defaults() -> None:
    p = Permissions()
    assert p.read is True
    assert p.glob is True
    assert p.grep is True
    assert p.codesearch is True
    assert p.semantic_search is False
    assert p.webfetch is False
    assert p.websearch is False
    assert p.task is False
    assert p.edit is True
    assert p.bash == {"*": "deny"}


def test_parse_simple_permissions() -> None:
    p = parse_permissions(
        {
            "read": "allow",
            "edit": "deny",
            "task": "deny",
        }
    )
    assert p.read is True
    assert p.edit is False
    assert p.task is False


def test_bash_allowlist_full_string_match() -> None:
    p = parse_permissions(
        {
            "bash": {
                "git status*": "allow",
                "git log*": "allow",
                "*": "deny",
            }
        }
    )
    # Простое совпадение.
    ok, pat = p.bash_allowed("git status")
    assert ok is True
    assert pat == "git status*"

    # Полная команда, не разрезаем по пайпам — git status* матчит весь паттерн.
    ok, _ = p.bash_allowed("git status | tee log")
    assert ok is True

    # Удалить — должно упасть в "*": deny.
    ok, pat = p.bash_allowed("rm -rf /")
    assert ok is False


def test_bash_no_allow_rules_denies_all() -> None:
    p = Permissions()  # default {"*": "deny"}
    ok, pat = p.bash_allowed("anything")
    assert ok is False
    assert pat == "*"


def test_edit_path_gating_specific_wins_over_wildcard() -> None:
    p = parse_permissions(
        {
            "edit": {
                "*": "deny",
                "orchx/_pending/plan.json": "allow",
                "orchx/runs/*/plan.json": "allow",
            }
        }
    )
    assert p.edit_allowed("orchx/_pending/plan.json") is True
    assert p.edit_allowed("orchx/runs/foo-bar/plan.json") is True
    assert p.edit_allowed("backend/foo.py") is False


def test_edit_bool_true_allows_anything() -> None:
    p = parse_permissions({"edit": "allow"})
    assert p.edit_allowed("any/path/wherever.py") is True


def test_edit_bool_false_denies_anything() -> None:
    p = parse_permissions({"edit": "deny"})
    assert p.edit_allowed("any/path.py") is False
