"""Тесты для извлечения фактов из notes воркеров (ANALYSIS.md §4 / §5.1.D)."""

from __future__ import annotations

from orchx.orchestrator.core import _extract_code_locations


def test_extract_simple_symbol_file_pair() -> None:
    notes = "Реализовал helper `process_cron_batch` в endpoints.py:4665."
    locs = _extract_code_locations(notes)
    assert any(
        loc["symbol"] == "process_cron_batch"
        and loc["file"] == "endpoints.py"
        and loc["line"] == 4665
        for loc in locs
    )


def test_extract_multiple_locations() -> None:
    notes = (
        "Перенёс `find_user` в src/users.py:120 и `update_user` "
        "в src/users.py:200; новые тесты в tests/test_users.py:50."
    )
    locs = _extract_code_locations(notes)
    syms = {loc["symbol"]: loc for loc in locs}
    assert "find_user" in syms
    assert syms["find_user"]["file"] == "src/users.py"
    assert syms["find_user"]["line"] == 120
    assert "update_user" in syms
    assert syms["update_user"]["line"] == 200


def test_extract_skips_short_symbols() -> None:
    notes = "поправил `a` в foo.py:1 — это шум."
    locs = _extract_code_locations(notes)
    assert all(loc["symbol"] != "a" for loc in locs)


def test_extract_skips_doc_files() -> None:
    notes = "обновил `что-то` в README.md:5"
    locs = _extract_code_locations(notes)
    assert all(not loc["file"].endswith(".md") for loc in locs)


def test_extract_handles_no_line() -> None:
    notes = "функция `helper_fn` в utils.py."
    locs = _extract_code_locations(notes)
    matches = [loc for loc in locs if loc["symbol"] == "helper_fn"]
    assert matches
    assert matches[0]["line"] is None


def test_extract_dedupes() -> None:
    notes = "`foo_bar` в src/x.py:10. И ещё раз `foo_bar` в src/x.py:10."
    locs = _extract_code_locations(notes)
    matches = [
        loc
        for loc in locs
        if loc["symbol"] == "foo_bar" and loc["file"] == "src/x.py"
    ]
    assert len(matches) == 1


def test_extract_empty_notes() -> None:
    assert _extract_code_locations("") == []
    assert _extract_code_locations(None) == []  # type: ignore[arg-type]


def test_extract_caps_at_50() -> None:
    notes = "; ".join(f"`sym_{i}` в file_{i}.py:1" for i in range(100))
    locs = _extract_code_locations(notes)
    assert len(locs) <= 50
