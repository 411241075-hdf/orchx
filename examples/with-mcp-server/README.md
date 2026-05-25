# orchX + MCP-bridge

orchX-воркеры подключаются к Model Context Protocol серверам — получают
доступ к их tools.

## Установка

```bash
pip install 'orchx[mcp]'
```

## Конфигурация роли

Добавьте `mcp_servers:` в frontmatter роли (например,
`.orchx/prompts/orchX-implementer.md`):

```markdown
---
description: implementer with GitHub MCP access
steps: 100
mcp_servers:
  - name: github
    command: npx
    args: [-y, "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: ${GITHUB_TOKEN}
  - name: fs
    command: npx
    args: [-y, "@modelcontextprotocol/server-filesystem", /path/to/project]
permission:
  read: allow
  edit: allow
  bash: {pytest: allow, npm: allow, "*": deny}
---

You are orchX-implementer with GitHub MCP integration.
Use tools like:
- github__list_issues  (proxy to MCP github server)
- github__create_issue
- fs__read_file
- fs__write_file
- (plus native: read, write, edit, glob, grep, bash)
```

## Что произойдёт

При спавне worker'а orchX:

1. Запустит каждый MCP-сервер как subprocess (через `npx` или указанный command).
2. Вызовет `tools/list` на каждом — получит список доступных tool'ов.
3. Префикснёт имена: `github__<tool_name>`, `fs__<tool_name>`.
4. Добавит их в registry рядом с native tools.
5. LLM видит и может звать любой из них.

При завершении worker'а — MCP-сессии закрываются через AsyncExitStack.

## Известные ограничения

* Только **stdio**-транспорт сейчас (URL/SSE будет добавлен в будущем).
* MCP-tools не gating'уются orchX permissions — гейтинг происходит
  внутри самого MCP-сервера. Будьте осторожны с правами файловой системы / GitHub-token'а / etc.
