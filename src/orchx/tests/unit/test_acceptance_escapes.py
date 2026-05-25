# -*- coding: utf-8 -*-
"""Тесты для _fix_literal_escapes_in_python_dash_c.

В прошлом прогоне (orchx/runs/admin-subdomain, задача remove-developer-panel)
planner-LLM выдал acceptance-команду с литеральным `\\n` (backslash+n) внутри
`python -c "..."`. POSIX sh не интерпретирует backslash-escape'ы внутри
двойных кавычек, поэтому Python получал физическую строку с обратным слешем
и падал с SyntaxError: unexpected character after line continuation
character. Задача функционально была выполнена, но acceptance проваливался
бесконечно. Эта функция превентивно нормализует такие команды на этапе
загрузки plan.json.
"""

from __future__ import annotations

from orchx.models import _fix_literal_escapes_in_python_dash_c as _fix


def test_python_dash_c_with_literal_backslash_n_is_converted() -> None:
    # Имитация реального сломанного plan'а:
    # python -c "import os\nif True:\n    print('OK')"
    # где \n — два символа (chr(92) + 'n').
    broken = (
        'python -c "import os' + chr(92) + 'n'
        'if True:' + chr(92) + 'n'
        '    print(' + chr(39) + 'OK' + chr(39) + ')"'
    )
    fixed = _fix(broken)
    assert "\n" in fixed
    # Литеральные \\n должны исчезнуть.
    assert (chr(92) + "n") not in fixed
    # Структура команды (prefix + quote + body + quote) сохранена.
    assert fixed.startswith('python -c "')
    assert fixed.endswith('"')


def test_python3_dash_c_also_handled() -> None:
    broken = 'python3 -c "import sys' + chr(92) + 'nprint(sys.version)"'
    fixed = _fix(broken)
    assert "\n" in fixed
    assert (chr(92) + "n") not in fixed


def test_node_dash_e_also_handled() -> None:
    broken = "node -e " + chr(39) + "console.log(1);" + chr(92) + "nconsole.log(2);" + chr(39)
    fixed = _fix(broken)
    assert "\n" in fixed


def test_real_newlines_already_present_are_preserved() -> None:
    # Если planner написал РЕАЛЬНЫЙ newline в JSON (как должен), мы его
    # не трогаем.
    correct = 'python -c "import os\nprint(os.getcwd())"'
    fixed = _fix(correct)
    assert fixed == correct


def test_no_backslash_n_outside_python_c_untouched() -> None:
    # Если \\n встречается ВНЕ `python -c`/`node -e` — например, в grep
    # regex'е — мы НЕ должны их трогать. Это могут быть валидные regex.
    cmd = "grep -E 'foo" + chr(92) + "nbar' file.txt"
    fixed = _fix(cmd)
    assert fixed == cmd


def test_command_without_python_c_returns_unchanged() -> None:
    # Самый частый кейс: простой shell-вызов, никаких python -c.
    cmd = "test -f frontend/src/pages/Analytics/components/DeveloperPanel.jsx"
    assert _fix(cmd) == cmd


def test_literal_backslash_t_also_converted() -> None:
    broken = 'python -c "x = 1' + chr(92) + 't' + chr(92) + 'nprint(x)"'
    fixed = _fix(broken)
    assert "\t" in fixed
    assert "\n" in fixed


def test_acceptance_check_uses_fix() -> None:
    """Полная цепочка: parse_acceptance с battle-проверенным регрессом."""
    from orchx.models import _parse_acceptance

    # Имитируем то, что прислал бы planner.
    raw = {
        "type": "command",
        "command": (
            'python -c "import os; p='
            + chr(39)
            + "foo.jsx"
            + chr(39)
            + chr(92) + "n"
            + "if os.path.isfile(p): print("
            + chr(39) + "OK" + chr(39) + ')"'
        ),
        "description": "test",
    }
    check = _parse_acceptance(raw)
    assert check.type == "command"
    assert "\n" in check.command
    assert (chr(92) + "n") not in check.command
