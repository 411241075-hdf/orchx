"""Permission-модель для воркера orchX.

Зеркалит то, что раньше делал kilo на основе frontmatter ``permission:``-блока.

Модель преднамеренно простая:

- Скаляр ``allow`` / ``deny`` для tool'ов без под-параметров (read, glob, …).
- Allow/deny + opt-glob-словарь для ``edit`` (path-gating).
- Allow-list-словарь команд для ``bash`` с **prefix-detection** и
  injection-guard'ом (default ``"*": deny``).

Bash sandbox матчит **извлечённый prefix** команды, а не полную строку.
Это закрывает дыру вида ``"git status* allow"`` пропускает ``git status &&
rm -rf /`` целиком — теперь композитные команды (``&&``, ``||``, ``;``,
``|``, backtick'и, ``$(...)``) явно отвергаются как command-injection.

Префикс — это первая (от 1 до 3) логических токенов команды без
ENV-вступления (``FOO=bar cmd`` → ``cmd``), без ``sudo``/``timeout``-
обёрток и без ``&&``/``;``/``|``-цепочек. Для команд с подкомандами
(``git status``, ``gh pr view``, ``npm run lint``) префикс — пара
«команда + первая subcommand'а», чтобы можно было allow'ить только
``git status`` и не давать ``git push``.

Совместимость со старыми spec'ами:

- Паттерны вроде ``"git status*"`` продолжают работать — суффиксный
  ``*`` мы воспринимаем как «всё, что начинается с этого prefix'а».
- Паттерны без хвоста (``"git status"``) — exact match по prefix'у.
- ``"*": "deny"`` — fallback (deny by default).
- ``"*": "allow"`` — wildcard-allow ВСЁ, кроме команд с injection'ом
  (используется только для `bash: allow` в frontmatter'е).
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

# Идентификатор «инъекция обнаружена», возвращаемый prefix-extractor'ом.
INJECTION_SENTINEL = "__command_injection_detected__"

# Идентификатор для безопасных read-only пайпов (см. SAFE_READONLY_PIPE_CMDS).
# Используется когда команда — pipe из read-only utility'ев (cmd1 | head, ls | grep).
SAFE_PIPE_PREFIX = "__safe_readonly_pipe__"

# Read-only утилиты, которые безопасны в любой комбинации через `|`.
# Эти команды не пишут в ФС, не делают сетевых запросов, не выполняют
# произвольный код. Если КАЖДЫЙ stage пайпа — из этого набора, мы
# трактуем весь пайп как одну read-only команду и обходим
# injection-guard. Это решает кейс типичных read-only пайпов:
# `ls foo | head`, `grep -rn pattern src | head -30`,
# `find . -name '*.py' | wc -l`.
SAFE_READONLY_PIPE_CMDS: frozenset[str] = frozenset(
    {
        # Поиск/обход
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "find",
        "fd",
        "fdfind",
        # Просмотр содержимого
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "bat",
        "tac",
        # Чтение метаданных
        "ls",
        "stat",
        "file",
        "wc",
        "du",
        "df",
        # Текстовая обработка (без -i / -e exec)
        "sort",
        "uniq",
        "cut",
        "tr",
        "rev",
        "column",
        "nl",
        "fold",
        "expand",
        "unexpand",
        "paste",
        "join",
        "comm",
        "tee",  # tee пишет, но в файлы — только если воркер указал;
        # в комбинации `cmd | tee /tmp/x` этот файл будет в worktree-cwd,
        # что корректно для read-only сценария логирования.
        "xargs",  # xargs опасен сам по себе, но без injection-операторов
        # внутри его аргументов (см. _strip_quoted) — ограниченно безопасен.
        "awk",  # awk без -f и без `system(...)` строится скриптом-аргументом,
        # injection-guard на `;` внутри строкового литерала уже учтён
        # через _strip_quoted.
        "sed",  # sed без -i и без `e` modifier'а — read-only.
        # Прочие утилиты, часто используемые в pipe'ах
        "echo",
        "printf",
        "yes",
        "true",
        "false",
        "date",
        "pwd",
        "id",
        "whoami",
        "uname",
        "hostname",
        "env",
        "printenv",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "tree",
        "diff",
        "patch",  # читает diff/файл, не выполняет код.
        "md5sum",
        "sha1sum",
        "sha256sum",
        "shasum",
        "od",
        "hexdump",
        "xxd",
        "strings",
        "python",
        "python3",
        # python в pipe — read-only ТОЛЬКО если первый аргумент `-c`
        # или скрипт внутри worktree. Мы пропускаем его в whitelist
        # под предположением, что injection-guard ловит явно
        # вредные паттерны (`os.system`, etc.) ВНЕ кавычек, а внутри
        # кавычек — это уже свойство самого скрипта, не shell injection.
        # Если воркер хочет писать через python — это не «pipe», а отдельный
        # вызов. Для pipe-сценариев типа `cat foo | python -c "import sys; …"`
        # это однозначно read-only по дизайну.
    }
)

# Cимволы/последовательности, которые делают команду композитной.
# Если они встречаются вне кавычек/heredoc'ов — это injection.
# Применяется к строке, где все quoted-сегменты (`'...'`, `"..."`)
# ЗАМЕНЕНЫ на нейтральный placeholder — поэтому `;` или `|` внутри
# `python -c "import re; ..."` не триггерят guard. См. _strip_quoted().
_INJECTION_OPERATORS = re.compile(
    r"""
    (?<!\\)`           # backtick command substitution
  | \$\(              # $(...) command substitution
  | \&\&              # AND chain
  | \|\|              # OR chain
  | (?<!\|)\|(?!\|)   # single pipe (но не часть || )
  | ;                 # statement separator
  | \n                # перевод строки внутри одной команды
  | >\(               # process substitution >(cmd)
  | <\(               # process substitution <(cmd)
    """,
    re.VERBOSE,
)


def _strip_quoted(cmd: str) -> str:
    """Заменить содержимое quoted-сегментов на placeholder, сохраняя длину.

    Это позволяет ``_INJECTION_OPERATORS.search`` НЕ срабатывать на
    `;`/`|`/`&&` ВНУТРИ строковых литералов (``python -c "import x; y"``,
    ``echo "a && b"``). Сами кавычки сохраняем, чтобы парсер видел
    структуру.

    Поддерживаются:
      - одинарные кавычки ``'...'`` (без интерпретации escape'ов внутри);
      - двойные кавычки ``"..."`` (escape ``\\"`` распознаётся).

    Heredoc'и (``<<EOF ... EOF``) намеренно НЕ обрабатываем — внутри
    них может прятаться настоящая инъекция, лучше пусть guard сработает.
    """
    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\" and i + 1 < n:
            # сохраняем escape-пару как есть
            out.append(ch)
            out.append(cmd[i + 1])
            i += 2
            continue
        if ch == "'":
            # single-quoted: найти закрывающую без интерпретации escape'ов
            out.append("'")
            j = i + 1
            while j < n and cmd[j] != "'":
                # внутри одинарных кавычек \ не является escape'ом
                out.append("X")
                j += 1
            if j < n:
                out.append("'")
                i = j + 1
            else:
                # незакрытая кавычка — bail out, отдадим исходник как есть,
                # дальше shlex.split упадёт и мы honestly вернём INJECTION_SENTINEL
                return cmd
            continue
        if ch == '"':
            out.append('"')
            j = i + 1
            while j < n:
                c2 = cmd[j]
                if c2 == "\\" and j + 1 < n:
                    out.append("XX")
                    j += 2
                    continue
                if c2 == '"':
                    break
                out.append("X")
                j += 1
            if j < n:
                out.append('"')
                i = j + 1
            else:
                return cmd  # незакрытая — fallthrough к guard'у
            continue
        out.append(ch)
        i += 1
    return "".join(out)

# Известные «обёртки», которые не делают команду опасной — мы прозрачно
# их перешагиваем при извлечении префикса. Для каждого: сколько следующих
# токенов оно «съедает» как свои опции.
# - ``sudo``: опции до первого не-флаговой токена.
# - ``timeout``: первый позиционный токен — длительность, потом сама команда.
# - env-prefix ``FOO=bar`` обрабатывается отдельно.
_PASSTHROUGH_PREFIXES = {"sudo", "timeout", "nice", "ionice", "env"}


# Команды, у которых allow-rule относится не к команде целиком, а к паре
# «команда + первая subcommand'а». Без этого `git push` нельзя отличить
# от `git status` через простой allowlist.
_TWO_TOKEN_COMMANDS = {
    "git",
    "gh",
    "docker",
    "kubectl",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "bun",
    "uv",
    "pip",
    "poetry",
    "cargo",
    "go",
    # Python: prefix включает первый аргумент после `-m`/`-c`/-script.
    # Это позволяет allow-list'у `"python -m": allow` сматчиться через
    # двухтокенный prefix `python -m`, и при этом не пропустить
    # `python /tmp/evil.py` (его prefix будет `python /tmp/evil.py` —
    # ни одно правило не сматчит).
    "python",
    "python3",
    # ruff: `ruff check` / `ruff format` — это субкоманды.
    "ruff",
}


def _is_safe_readonly_pipe(cmd: str) -> bool:
    """Распознать «безопасный read-only пайп»: ``cmd1 | cmd2 | …``.

    Условия:

    - Команда содержит ТОЛЬКО pipe-разделители (``|``), без ``&&``, ``||``,
      ``;``, ``$(...)``, backtick'ов, process-substitution.
    - Каждый stage пайпа начинается с команды из
      :data:`SAFE_READONLY_PIPE_CMDS`.

    Это решает фантомный deny на безопасные команды вроде
    ``ls foo | head -30``, ``grep -rn pattern src | head -30``,
    ``find . -name '*.py' | wc -l``.
    """
    stripped = _strip_quoted(cmd)
    # Должны быть ТОЛЬКО pipe-разделители. Любая другая композиция
    # (``;``, ``&&``, ``||``, ``$(``, ``\``) — это injection.
    forbidden = re.compile(
        r"""
        (?<!\\)`           # backtick
      | \$\(              # $(...)
      | \&\&              # AND
      | \|\|              # OR
      | ;                 # statement separator
      | \n
      | >\(               # process substitution
      | <\(
        """,
        re.VERBOSE,
    )
    if forbidden.search(stripped):
        return False
    if "|" not in stripped:
        return False
    # Разбиваем по pipe (но не на ``||`` — мы только что проверили его отсутствие).
    stages = [s.strip() for s in stripped.split("|") if s.strip()]
    if len(stages) < 2:
        return False
    for stage in stages:
        try:
            stage_tokens = shlex.split(stage, posix=True)
        except ValueError:
            return False
        if not stage_tokens:
            return False
        # Снимаем env-prefix (FOO=bar cmd ...).
        while stage_tokens and re.match(
            r"^[A-Za-z_][A-Za-z0-9_]*=", stage_tokens[0]
        ):
            stage_tokens.pop(0)
        if not stage_tokens:
            return False
        head_tok = stage_tokens[0]
        if head_tok not in SAFE_READONLY_PIPE_CMDS:
            return False
    return True


def extract_command_prefix(command: str) -> str:
    """Извлечь канонический prefix команды для матчинга.

    Возвращает:

    - ``"<INJECTION_SENTINEL>"``, если в команде обнаружена композиция
      (``&&``, ``||``, ``;``, backtick'и, ``$(...)``).
    - ``"<SAFE_PIPE_PREFIX>"``, если команда — безопасный pipe из
      read-only утилит (``ls | head``, ``grep | wc``).
    - Пустую строку для команд без prefix'а (например, чистый ``$(echo)``,
      пустая команда). Вызывающий код трактует как «нет правила, deny».
    - Один токен (``"ls"``, ``"cat"``) для команд без подкоманд.
    - Два токена (``"git status"``, ``"gh pr"``, ``"npm run"``) для
      команд из ``_TWO_TOKEN_COMMANDS``.

    Поведение совместимо с эталонной prefix-detection из
    ``examples/agent-prompt-bash-command-prefix-detection.md`` (Claude Code).

    Args:
        command: shell-команда целиком.
    """
    cmd = command.strip()
    if not cmd:
        return ""

    # Спец-кейс: безопасный read-only pipe (см. SAFE_READONLY_PIPE_CMDS).
    # Если команда содержит ТОЛЬКО `|` и каждый stage — read-only утилита,
    # пропускаем injection-guard.
    if _is_safe_readonly_pipe(cmd):
        return SAFE_PIPE_PREFIX

    # Грубая проверка на injection-операторы. Делается ДО shlex, чтобы
    # не зависеть от его токенизации.
    # ВАЖНО: применяем регексп к версии команды, где quoted-сегменты
    # заменены на placeholder'ы. Иначе `python -c "import re; print(...)"`
    # ложно срабатывает на `;` внутри строкового литерала — в прошлых
    # прогонах это блокировало >30 шагов воркеров (см.
    # api-admin-db.attempt2.log: десятки попыток обойти запрет на `;`).
    if _INJECTION_OPERATORS.search(_strip_quoted(cmd)):
        return INJECTION_SENTINEL

    # Токенизируем shell-style. Если shlex не справился (битые кавычки) —
    # отвергаем, лучше параноить.
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return INJECTION_SENTINEL
    if not tokens:
        return ""

    # Шаг 1 — снимаем env-prefix (FOO=bar BAZ=qux cmd ...).
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    if not tokens:
        return ""

    # Шаг 2 — снимаем passthrough-обёртки (sudo, timeout, env, nice, ...).
    # Сначала съедаем все ведущие `-...` опции (с возможным аргументом).
    # Затем для команд, у которых первый позиционный аргумент — не команда
    # (timeout 30, nice -n 10 НЕ съели в опции, etc.), съедаем дополнительно
    # один позиционный токен.
    while tokens and tokens[0] in _PASSTHROUGH_PREFIXES:
        wrapper = tokens.pop(0)
        # Шаг 2a — флаги (включая флаги-с-аргументом).
        while tokens and tokens[0].startswith("-"):
            opt = tokens.pop(0)
            # Опции с аргументом, известные для sudo/env/nice/ionice/timeout.
            opts_with_arg = {
                "sudo": {"-u", "-g", "-h", "-p", "-S"},
                "env": {"-u", "-S"},
                "timeout": {"-s", "-k"},
                "nice": {"-n"},
                "ionice": {"-c", "-n", "-p", "-P", "-u"},
            }.get(wrapper, set())
            if opt in opts_with_arg and tokens:
                tokens.pop(0)
        # Шаг 2b — для timeout/nice/ionice без `-n N` синтаксиса — берём
        # первый позиционный «не-команду». timeout: первая длительность
        # ('30', '5m'); nice: positive integer; ionice: число.
        if wrapper in {"timeout", "nice", "ionice"} and tokens:
            # Если первый токен похож на «значение» (число, возможно с
            # суффиксом времени), съедаем его. Если это уже сама команда
            # (буквенное слово) — оставляем.
            head = tokens[0]
            if re.match(r"^\d+([smhd]|\.\d+)?$", head):
                tokens.pop(0)

    if not tokens:
        return ""

    head = tokens[0]
    if head in _TWO_TOKEN_COMMANDS and len(tokens) >= 2:
        sub = tokens[1]
        # Спец-кейс для python/python3: `-m`, `-c`, `-u` — фактически
        # subcommand'ы (выбирают режим), не просто опции. Включаем их
        # в двухтокенный prefix целиком: `python -m pytest` → `python -m`,
        # `python -c "..."` → `python -c`, `python script.py` → `python script.py`.
        if head in {"python", "python3"} and sub in {"-m", "-c"}:
            return f"{head} {sub}"
        # Если subcommand — не флаг, формируем двухтокенный prefix.
        # `git --version` → один токен `git`.
        if not sub.startswith("-"):
            return f"{head} {sub}"
    return head


@dataclass
class BashRuleHit:
    """Что вернул матчер для одного вызова."""

    allowed: bool
    pattern: str | None
    prefix: str
    """Извлечённый prefix (или INJECTION_SENTINEL/пустая строка)."""
    reason: str
    """Человеко-читаемая причина решения (для error-сообщения воркеру)."""


def _truthy(val: Any, default: bool) -> bool:
    """Прочитать allow/deny-скаляр (str) или bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() == "allow"
    return default


# Поля frontmatter'а, которые orchX runtime реально использует.
# Всё, что не в этом списке, тихо игнорируется (с warning в логе).
_KNOWN_PERMISSION_KEYS = frozenset(
    {
        "read",
        "glob",
        "grep",
        "codesearch",
        "semantic_search",
        "webfetch",
        "websearch",
        "task",
        "edit",
        "bash",
    }
)

# Поля frontmatter'а, которые остались в spec'ах от kilo, но orchX их
# не использует. Их парсим тихо (не warn'им) — иначе старые spec'ы
# будут производить шум в логах.
_LEGACY_PERMISSION_KEYS = frozenset({"doom_loop", "lsp"})


@dataclass
class Permissions:
    """Разрешения, выводящиеся из frontmatter agent-файла."""

    read: bool = True
    glob: bool = True
    grep: bool = True
    semantic_search: bool = False
    codesearch: bool = True
    lsp: bool = False
    """P1.6: разрешён ли набор symbol-tools (find_symbol / find_references /
    rename_symbol). По умолчанию off (новый capability, opt-in)."""
    browser: bool = False
    """P1.7: разрешён ли browser tool (Playwright)."""
    webfetch: bool = False
    websearch: bool = False
    task: bool = False
    edit: bool | dict[str, str] = True
    """Если ``True`` — разрешён любой путь. Если dict — glob → ``allow``/``deny``.

    В dict-форме порядок значений — самые специфичные паттерны первыми;
    ``"*"`` идёт последним fallback'ом.
    """

    bash: dict[str, str] = field(default_factory=lambda: {"*": "deny"})
    """Allow-list bash-команд в формате glob → allow/deny.

    Матчинг идёт по ИЗВЛЕЧЁННОМУ PREFIX'у команды (не по полной строке).
    Композитные команды (``&&``, ``|``, ``;``, backtick'и, ``$(...)``)
    отвергаются как injection до матча.
    """

    def edit_allowed(self, rel_path: str) -> bool:
        """Разрешено ли редактировать файл по относительному пути."""
        if isinstance(self.edit, bool):
            return self.edit
        rules = sorted(
            self.edit.items(),
            key=lambda kv: ("*" in kv[0], -len(kv[0])),
        )
        for pattern, action in rules:
            if fnmatch.fnmatchcase(rel_path, pattern):
                return action == "allow"
        return False

    def bash_allowed(self, command: str) -> tuple[bool, str | None]:
        """Совместимый с прежней сигнатурой матчер. Возвращает (allowed, pattern).

        Для подробного результата (с причиной отказа) используй
        :meth:`bash_check`.
        """
        hit = self.bash_check(command)
        return (hit.allowed, hit.pattern)

    def bash_check(self, command: str) -> BashRuleHit:
        """Подробная проверка команды.

        Алгоритм:

        1. Извлекаем prefix через :func:`extract_command_prefix`.
        2. Если prefix == INJECTION_SENTINEL — отвергаем сразу с причиной
           «command injection detected».
        3. Если prefix пустой — отвергаем (не смогли разобрать команду).
        4. Иначе матчим prefix против правил из ``self.bash``. Правила
           сортируются по специфичности (длина без ``*``).

        **Совместимость со старыми правилами**: правило ``"git status*"``
        в legacy-spec'ах применяется как «всё, что начинается с
        ``git status``», т.е. нормализованное «exact = git status или
        что-то после, например `git status -u`». Для prefix'а ``git status``
        паттерн ``git status*`` всё равно matched через `fnmatchcase`.

        ``"*": allow`` — wildcard, матчит ЛЮБОЙ извлечённый prefix.
        Композитные команды всё равно отвергаются injection-guard'ом.
        """
        prefix = extract_command_prefix(command)
        if prefix == INJECTION_SENTINEL:
            return BashRuleHit(
                allowed=False,
                pattern=None,
                prefix=prefix,
                reason=(
                    "command injection detected: command contains shell "
                    "metacharacters (&&, ||, ;, backticks, or $(…)). "
                    "Run only one command at a time, without chaining. "
                    "Hint: read-only pipes between safe utilities "
                    "(grep/find/ls/cat/head/tail/wc/sort/awk/sed/etc.) "
                    "ARE allowed — use simple `cmd1 | cmd2` syntax."
                ),
            )
        if prefix == SAFE_PIPE_PREFIX:
            # Безопасный read-only pipe — пропускаем по умолчанию, т.к. он
            # уже верифицирован _is_safe_readonly_pipe() (каждая стадия
            # из SAFE_READONLY_PIPE_CMDS). Дефолт ``"*": "deny"`` НЕ
            # применяется — иначе мы бы воспроизвели старое поведение
            # «любой pipe запрещён», ради которого этот класс safe-pipe'ов
            # и был введён.
            #
            # Уважаем ТОЛЬКО явные правила-маркеры, относящиеся именно к
            # pipe'ам: ``"|": "deny"`` или ``"safe_pipe": "deny"``. Эти
            # маркеры воркер/конфиг может добавить, если по какой-то
            # причине pipe'ы небезопасны в его сценарии.
            for pat, action in self.bash.items():
                if pat in {"|", "safe_pipe"} and action == "deny":
                    return BashRuleHit(
                        allowed=False,
                        pattern=pat,
                        prefix=prefix,
                        reason=f"safe read-only pipe denied by pattern {pat!r}",
                    )
            return BashRuleHit(
                allowed=True,
                pattern="<safe_readonly_pipe>",
                prefix=prefix,
                reason=(
                    "safe read-only pipe (every stage is a known read-only utility "
                    "from SAFE_READONLY_PIPE_CMDS)"
                ),
            )
        if not prefix:
            return BashRuleHit(
                allowed=False,
                pattern=None,
                prefix="",
                reason="empty or unparseable command",
            )

        # Сортировка: самые специфичные первыми, "*" — в конец.
        # Длина без `*` — индикатор «специфичности».
        rules = sorted(
            self.bash.items(),
            key=lambda kv: (kv[0] == "*", -len(kv[0].replace("*", ""))),
        )
        for pattern, action in rules:
            # Normalize legacy patterns: `"ls *"` and `"git status*"` both
            # written by humans for full-string matching, here we match by
            # prefix. Strip trailing whitespace+star and a trailing star so
            # `"ls *"`, `"ls*"` and `"ls"` all describe «prefix == ls».
            norm = pattern.rstrip()
            if norm.endswith("*"):
                norm = norm[:-1].rstrip()
            if not norm:
                # Только `*` — wildcard, отдельная ветка матчинга.
                if pattern == "*" and fnmatch.fnmatchcase(prefix, "*"):
                    return BashRuleHit(
                        allowed=action == "allow",
                        pattern=pattern,
                        prefix=prefix,
                        reason=(f"prefix={prefix!r} matched wildcard '*' → {action}"),
                    )
                continue
            if prefix == norm or fnmatch.fnmatchcase(prefix, norm):
                return BashRuleHit(
                    allowed=action == "allow",
                    pattern=pattern,
                    prefix=prefix,
                    reason=(
                        f"prefix={prefix!r} matched pattern {pattern!r} "
                        f"(normalized: {norm!r}) → {action}"
                    ),
                )

        return BashRuleHit(
            allowed=False,
            pattern=None,
            prefix=prefix,
            reason=f"no rule matched prefix={prefix!r}",
        )


def parse_permissions(raw: dict[str, Any]) -> Permissions:
    """Сконвертировать frontmatter-словарь в :class:`Permissions`.

    Неизвестные ключи (кроме legacy-набора `doom_loop`/`lsp`) игнорируются
    без warning'а — orchX runtime использует только поля из
    :data:`_KNOWN_PERMISSION_KEYS`. В будущем можно добавить strict-режим
    через env, чтобы ошибки в spec'е ловились на CI.
    """
    p = Permissions()
    p.read = _truthy(raw.get("read", "allow"), True)
    p.glob = _truthy(raw.get("glob", "allow"), True)
    p.grep = _truthy(raw.get("grep", "allow"), True)
    p.semantic_search = _truthy(raw.get("semantic_search", "deny"), False)
    p.codesearch = _truthy(raw.get("codesearch", "allow"), True)
    p.lsp = _truthy(raw.get("lsp", "deny"), False)
    p.browser = _truthy(raw.get("browser", "deny"), False)
    p.webfetch = _truthy(raw.get("webfetch", "deny"), False)
    p.websearch = _truthy(raw.get("websearch", "deny"), False)
    p.task = _truthy(raw.get("task", "deny"), False)

    edit_raw = raw.get("edit", "allow")
    if isinstance(edit_raw, dict):
        # Сохраняем как есть: glob → "allow"/"deny" (строки).
        p.edit = {str(k): str(v).strip().lower() for k, v in edit_raw.items()}
    else:
        p.edit = _truthy(edit_raw, True)

    bash_raw = raw.get("bash")
    if isinstance(bash_raw, dict):
        p.bash = {str(k): str(v).strip().lower() for k, v in bash_raw.items()}
    elif isinstance(bash_raw, (str, bool)):
        # bash: allow|deny — превратим в один шаблон "*".
        allowed = _truthy(bash_raw, False)
        p.bash = {"*": "allow" if allowed else "deny"}
    else:
        p.bash = {"*": "deny"}
    return p


def describe_permissions(p: Permissions) -> str:
    """Человекочитаемое описание для system-prompt'а."""
    lines: list[str] = []
    flags = [
        ("read", p.read),
        ("glob", p.glob),
        ("grep", p.grep),
        ("codesearch", p.codesearch),
    ]
    if p.semantic_search:
        flags.append(("semantic_search", True))
    lines.append("- Read tools: " + ", ".join(name for name, ok in flags if ok))
    if isinstance(p.edit, bool):
        lines.append(f"- edit: {'allowed' if p.edit else 'DENIED'}")
    else:
        allowed = [g for g, a in p.edit.items() if a == "allow"]
        denied = [g for g, a in p.edit.items() if a == "deny"]
        lines.append(
            "- edit: path-gated (allowed: "
            + (", ".join(allowed) or "—")
            + (f"; denied: {', '.join(denied)}" if denied else "")
            + ")"
        )
    allowed_bash = [g for g, a in p.bash.items() if a == "allow"]
    if allowed_bash:
        lines.append(
            "- bash: prefix-matched allow-list — "
            + ", ".join(sorted(allowed_bash))
            + ". Composite commands (&&, ||, ;, $(...), backticks) are "
            + "blocked as command injection. **Read-only pipes** between "
            + "safe utilities (grep/find/ls/cat/head/tail/wc/sort/awk/sed/"
            + "uniq/cut/tr/xargs/diff/...) ARE allowed — use simple "
            + "`cmd1 | cmd2` syntax for `ls | head`, `grep -rn x src | wc -l`, etc."
        )
    else:
        lines.append("- bash: no commands allowed")
    return "\n".join(lines)
