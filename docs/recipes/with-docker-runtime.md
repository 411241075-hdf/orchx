# Recipe — Docker runtime (sandboxed worker)

> Запускать orchX-worker'ов в Docker-контейнерах. Даёт изоляцию от
> malicious-кода, reproducible-окружение, чистый rollback.

## 1. Установить extras

```bash
pip install 'orchx[docker]'
```

## 2. Собрать worker image

В корне репо:

```bash
make worker-image
# Эквивалент:
# docker build -f src/orchx/templates/runtime/Dockerfile.worker -t orchx-worker:latest .
```

Image содержит:

- Python 3.11 + git + ripgrep + curl
- orchx + все зависимости

## 3. Config

`.orchx/config.yaml`:

```yaml
runtime: docker

plugin_config:
  docker:
    image: orchx-worker:latest
    network: none # default; "host" для интернета
    cpu_quota: "2" # 2 CPU per worker
    memory: "2g" # 2 GB
    env_passthrough:
      - OPENAI_API_KEY
      - OPENAI_BASE_URL
      - ORCHX_LLM_MODEL
      - ORCHX_PLANNER_MODEL
```

## 4. Запустить

```bash
orchx all "Refactor authentication module"
```

orchX будет спавнить worker'ов внутри контейнеров с **read-only** mount'ом
исходного репо и RW worktree. Никакой worker не сможет дотянуться вне
своего worktree (даже если попытается).

## Trade-offs

| Pro                                      | Con                                         |
| ---------------------------------------- | ------------------------------------------- |
| Изоляция (защита от malicious кода)      | Cold-start ~2-5s per worker                 |
| Reproducible env                         | Лишний disk-space (image)                   |
| Чистый rollback (контейнер удалён после) | Не работает без Docker daemon'а             |
| Network-policy на уровне runtime         | Network-only задачи требуют `network: host` |
