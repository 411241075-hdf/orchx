# Recipe — GitHub Projects v2 как tracker

> orchX будет брать задачи из колонки `Ready` вашего GitHub Project,
> атомарно двигать их в `In Progress`, при завершении прогона
> комментировать связанный issue и передвигать карточку в `Done`.

## 1. Что должно быть готово

1. **Projects v2 в репо/организации** с полем статуса `Status`
   (single-select) и опциями `Ready`, `In Progress`, `Done`.
   Имена колонок настраиваются — см. конфиг ниже.
2. **`gh` CLI** установлен и авторизован:

   ```bash
   gh auth login
   gh auth refresh -s project,read:project  # scopes для Projects v2
   ```

3. Номер проекта — из URL:
   * Organization-level: `https://github.com/orgs/<ORG>/projects/<NUMBER>`.
   * User-level:         `https://github.com/users/<USER>/projects/<NUMBER>`.

## 2. Конфиг

`.orchx/config.yaml`:

```yaml
tracker: github-projects

plugin_config:
  github-projects:
    # Для org-level project укажите ``orgs/<org>``; для user-level — пропустите,
    # orchX возьмёт первую часть owner_repo.
    project_owner: orgs/my-org
    project_number: 7
    # Имена колонок (по умолчанию — стандартные).
    status_field: Status
    ready_column: Ready
    in_progress_column: In progress
    done_column: Done
```

## 3. CLI-команды

### Просмотр Ready

```bash
orchx tasks ready
```

```text
 orchX tasks: 3 ready
  • [PVTI_lAH...:123] Add password reset email template
      https://github.com/my-org/api/issues/123
  • [PVTI_lAH...:124] Rate-limit /login endpoint
  • [PVTI_lAH...:125] Document JWT refresh flow
```

### Pick — взять первую задачу из Ready

```bash
orchx tasks pick
```

orchX атомарно передвинет карточку в `In Progress` и напечатает
заголовок + body. Это удобно скриптовать:

```bash
TASK_BODY=$(orchx tasks pick | sed -n '/--- task body ---/,/^---/{//!p;}')
orchx all "$TASK_BODY"
```

### Move — вручную передвинуть карточку

```bash
orchx tasks move "PVTI_lAH...:123" "Done"
```

## 4. Автоматический workflow

При запуске `orchx run` / `orchx all`:

* На старте — `tracker.update_status(task_id, "running")` → комментарий
  на issue + перенос карточки в `In Progress`.
* На финише — `"done"` или `"failed"` с детальной разбивкой
  (`success/failed/skipped/cost`) + перенос в `Done` (если success).

### Демон, который непрерывно работает над Ready

```bash
while true; do
  PICKED=$(orchx tasks pick 2>&1)
  if echo "$PICKED" | grep -q "Ready column is empty"; then
    sleep 60
    continue
  fi
  TITLE=$(echo "$PICKED" | sed -n 's/^Title: //p')
  BODY=$(echo "$PICKED" | sed -n '/--- task body ---/,/^---/{//!p;}')
  orchx all "$TITLE: $BODY"
done
```

## 5. Формат `task_id` (composite)

`pick`/`ready` возвращают composite ID `<project_item_id>:<issue_number>`.
* `project_item_id` нужен для GraphQL мутаций (move).
* `issue_number` — для комментариев через `gh issue comment`.

`move <id> <column>` принимает composite ID. `update_status` тоже
поддерживает оба формата (composite — лучше).

## 6. Troubleshooting

* **`status field 'Status' not found`** — проверьте имя single-select
  поля в проекте: оно регистрозависимое.
* **`column 'Ready' not found`** — проверьте имена опций в Status
  field; они тоже регистрозависимые. Можно переопределить через
  `ready_column`/`in_progress_column`/`done_column`.
* **`HTTP 401/403`** от `gh api graphql` — нужны scopes
  `project` и `read:project` (для org-projects), и `repo` для
  комментариев на issue.
