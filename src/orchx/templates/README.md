# `.orchx/` — runtime каталог orchX

Эта папка создана командой `orchx init`. Что лежит:

| Путь | Назначение | Версионируется в git? |
|---|---|---|
| `.env` | Конфиг LLM Proxy (URL, API-key, модель) | **Нет** (gitignore) |
| `.env.example` | Пример `.env` для других разработчиков | Да |
| `PROJECT.md` | Описание стека/конвенций проекта (читают все роли) | Да |
| `prompts/` | Кастомизированные промпты ролей (если редактировал) | Да |
| `runs/<task_id>/` | Артефакты прогона (логи, plan.json, summary.json) | **Нет** (gitignore) |
| `_pending/` | Staging для `orchx plan` пока task_id не известен | **Нет** (gitignore) |

## Quick start

```bash
# 1. Заполни .env (один раз).
cp .orchx/.env.example .orchx/.env
$EDITOR .orchx/.env   # ORCHX_LLM_BASE_URL, ORCHX_LLM_API_KEY, ORCHX_MODEL

# 2. Опиши свой проект (один раз).
$EDITOR .orchx/PROJECT.md

# 3. Запусти рой.
orchx all "Реализуй фичу X"
```

## Кастомизация промптов

По умолчанию рой использует промпты, шиппящиеся с пакетом orchx
(`<package>/templates/prompts/orchX-*.md`).

Чтобы переопределить роль для своего проекта:

1. Скопируй нужный промпт из пакета в `.orchx/prompts/`:
   ```bash
   python -c "import orchx, pathlib, shutil; \
     src = pathlib.Path(orchx.__file__).parent / 'templates/prompts/orchX-implementer.md'; \
     shutil.copy(src, '.orchx/prompts/orchX-implementer.md')"
   ```
2. Отредактируй копию.
3. Запусти `orchx all "<task>"` — рой подхватит твою версию вместо дефолтной.

Каскад поиска: `.orchx/prompts/orchX-<role>.md` → `<package>/templates/prompts/orchX-<role>.md`.

## Очистка

```bash
# Старый run (можно сносить — все артефакты внутри run_dir).
rm -rf .orchx/runs/<task_id>

# Все runtime-артефакты разом.
rm -rf .orchx/runs/ .orchx/_pending/
```

`.env` и `PROJECT.md` останутся.

## Обновление пакета

```bash
pip install --upgrade orchx
# Если хочешь подтянуть свежие дефолты промптов в свою кастомизацию:
orchx init --force      # перезапишет .orchx/prompts/ из новой версии пакета
```
