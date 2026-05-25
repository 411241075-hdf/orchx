# orchX hello-world

Минимальный пример: одна задача, FLAT-план, никаких extras.

## Prep

```bash
cd /tmp
git init hello-orchx && cd hello-orchx
git commit --allow-empty -m "init"

# Установка orchX:
pip install orchx
orchx init

# Заполните .orchx/.env переменными ORCHX_LLM_* для своего LLM provider'а.
cp .orchx/.env.example .orchx/.env
# vim .orchx/.env
```

## Запуск

```bash
orchx all "Create a Python module 'greet.py' with function greet(name: str) -> str that returns 'Hello, {name}!'"
```

## Что произойдёт

1. orchX-planner сгенерирует `orchx/runs/<task_id>/plan.json` (1 задача).
2. Создастся integration-ветка `orchX/<task_id>` + worktree.
3. orchX-implementer напишет `greet.py`.
4. acceptance: проверка что файл существует с нужной сигнатурой.
5. orchX-reviewer прочитает дифф и выпустит summary.
6. `gh pr create` откроет PR.

Все логи — в `orchx/runs/<task_id>/`.
