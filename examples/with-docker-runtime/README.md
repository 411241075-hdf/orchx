# orchX + Docker runtime

См. [docs/recipes/with-docker-runtime.md](../../docs/recipes/with-docker-runtime.md)
для полной инструкции.

## TL;DR

```bash
pip install 'orchx[docker]'
make worker-image  # docker build orchx-worker:latest

cat > .orchx/config.yaml << EOF
runtime: docker
plugin_config:
  docker:
    image: orchx-worker:latest
    network: none
EOF

orchx all "Refactor X to Y"
```

orchX будет запускать каждого worker'а внутри отдельного контейнера с
read-only mount'ом repo и RW worktree.
