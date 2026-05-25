# orchX vs OpenHands vs Ruflo vs ComposioHQ/agent-orchestrator — сравнительный анализ

Документ описывает позиционирование, архитектуру, сильные/слабые стороны
четырёх систем мультиагентной разработки и в финале выделяет, чем именно
orchX отличается и где у него пробелы.

## 1. Tl;dr

| Проект            | Сущность                                                           | Стек                | License                      | Stars  |
| ----------------- | ------------------------------------------------------------------ | ------------------- | ---------------------------- | ------ |
| **orchX**         | Headless Python CLI: ТЗ → DAG → параллельные воркеры → PR          | Python 3.13+        | MIT                          | ранний |
| **OpenHands**     | Полноценная агентная платформа (SDK + GUI + Cloud)                 | Python + TS/React   | MIT (+ Enterprise: Polyform) | 74.8k  |
| **Ruflo**         | Оркестратор поверх Claude Code: 100+ агентов, federation, learning | TypeScript (+ Rust) | MIT                          | 54.9k  |
| **AO (Composio)** | Dashboard для управления флотом параллельных агентов               | TypeScript (Node)   | MIT                          | 7.3k   |

**Краткое позиционирование:**

- **orchX** — узкоспециализированный **batch-планировщик** для git-репо:
  «дай ТЗ → получи PR». В нём минимум UI, нет долгоживущих процессов,
  нет federation/memory; вся логика — около `planner → DAG → worker →
merge → PR`. Это «компилятор ТЗ в pull request».
- **OpenHands** — **полная экосистема**: SDK + CLI + GUI + Cloud +
  Enterprise. Один агент с богатым окружением (бэйзлайн SOTA на
  SWE-bench 77.6%). Поддерживает Docker-runtime, browser, многомодельность.
  Multi-agent — опционально, но не центр продукта.
- **Ruflo** (бывший Claude-Flow) — **swarm-надстройка над Claude Code**:
  314 MCP-tools, AgentDB с HNSW-векторным поиском, SONA self-learning,
  zero-trust federation между машинами, 30+ плагинов. Это «нервная
  система» поверх существующего IDE-agent'а.
- **AO** — **operator's dashboard** для параллельных агентов: каждый агент
  в своём worktree, автоматическая реакция на CI failures / review
  comments. Plugin-based (runtime / agent / workspace / tracker /
  notifier / terminal slots).

## 2. Архитектура

### 2.1 orchX

```text
task / spec
    │
    ▼
planner ──► plan.json (FLAT или PHASED)
    │
    ▼
orchestrator
    │
┌───┴────────────┐
│  phase loop    │ ── между фазами merge в integration-ветку
│  (последоват.) │
└───┬────────────┘
    │ внутри фазы — DAG по level'ям
    │
    ▼
worker (in-process, asyncio) × N в git worktree'ах
    │
    ▼
merge → integration branch → push → gh pr create
```

**Ключевые свойства:**

- Standalone Python-процесс, никаких daemon'ов.
- Воркер = `asyncio`-корутина в **отдельном git worktree** + свой `LLMClient`.
- LLM-bridge — OpenAI-совместимый Proxy (`ORCHX_LLM_BASE_URL`).
- Tool registry: `read/write/edit/glob/grep/codesearch/bash/todowrite/task/webfetch`.
- Permissions:
  - `bash` — prefix-allow-list + injection-guard (`&&`, `;`, `|`, backticks автоматически блокируются).
  - `edit` — path-gated, sandbox строго внутри `cwd` worktree'а.
- Compaction: при достижении ~75% context window — summary-проход.
- Effort-маппинг: `low|medium|high|xhigh|max` → провайдер-специфичные
  поля (`reasoning_effort` для OpenAI, `thinking` для Claude,
  `thinking_config.thinking_budget` для Gemini).
- Replan: при провале фазы (`allow_replan: true`) — `planner` вызывается
  повторно с контекстом провала, до `max_replans` раз.

### 2.2 OpenHands

```text
Interfaces (GUI / CLI / Custom)
       │
       ▼
openhands.sdk  ◄── Workspace (Local / Docker / RemoteAPI)
   │  │  │                                │
   │  │  └── Agent (reasoning-action loop)│
   │  └──── Conversation / Events / Skills / Condenser / Security
   └─── LLM (multi-provider)
       │
       ▼
openhands.tools  (BashTool, FileEditor, GrepTool, MCP integration, …)
       │
       ▼
openhands.agent_server (FastAPI + WebSocket для container/remote)
```

**Ключевые свойства:**

- **4-package SDK**: `sdk`, `tools`, `workspace`, `agent_server` —
  чёткое разделение API/реализации/runtime/сервера.
- Stateless, **immutable Pydantic models**, type-safe by default.
- Один и тот же agent code работает в Local / Docker / Remote режиме —
  меняется только класс Workspace.
- **Microagents** — конфигурируемые prompt-extension (markdown + YAML
  frontmatter с триггерами), загружаются из `microagents/` и
  `.openhands/microagents/`.
- **Skill / Condenser / Security** — built-in: skill triggers,
  conversation compression (managed token budget), action risk
  assessment.
- **REST API + WebSocket** — production-ready, multi-user.
- VSCode-расширение, Slack/Jira/Linear/Stripe-интеграции в `enterprise/`.
- Benchmarks: SWE-bench 77.6%, SWT-bench, multi-SWE-bench.

### 2.3 Ruflo

```text
User → CLI / MCP → Router → Swarm → Agents → Memory → LLM Providers
                  ^                                │
                  └── Learning loop (SONA + HNSW) ─┘
```

**Ключевые свойства:**

- **Hierarchical / Mesh / Adaptive swarm topology**, queen-led
  координация, **5 consensus стратегий** (Byzantine, Raft, Gossip,
  CRDT, Quorum).
- **AgentDB + HNSW** — векторная память (150x–12,500x быстрее brute
  force), shared между сессиями.
- **SONA** — Self-Optimizing Neural Architecture, <0.05ms adaptation,
  ReasoningBank pattern store.
- **17 hooks + 12 background workers** (audit, optimize, testgaps,
  refactor, etc.) — реагируют на события сами.
- **Agent Federation** — zero-trust mTLS + ed25519 между разными
  машинами/организациями, PII redaction pipeline, behavioral trust
  scoring.
- **3-tier model routing**: Agent Booster (WASM, <1ms, $0) → Haiku
  ($0.0002) → Sonnet/Opus ($0.003–0.015).
- **314 MCP tools**, 100+ specialized agents, 32 plugins, 26 CLI commands.
- **Dual-mode**: Claude Code 🔵 + Codex 🟢 параллельно, shared memory
  namespace, cross-platform learning.
- **Named agents + SendMessage** — agents общаются напрямую друг с другом
  (pipeline / fan-out / supervisor patterns).
- **Web UI** ([flo.ruv.io](https://flo.ruv.io)) + **GOAP planner**
  ([goal.ruv.io](https://goal.ruv.io)) с A\*-поиском в state-space.

### 2.4 ComposioHQ/agent-orchestrator (AO)

```text
ao start
   │
   ▼
Orchestrator agent (knows what to do, uses AO CLI internally)
   │
   ├── spawn worker (each in own git worktree)
   ├── react to CI failure → send-to-agent
   ├── react to changes-requested → send-to-agent
   └── react to approved-and-green → notify (or auto-merge)
   │
   ▼
Dashboard (Next.js 15 + React 19 + Tailwind v4): kanban с 6 attention-priority колонками
```

**Ключевые свойства:**

- **Plugin architecture (7 slots)**:
  - Runtime: tmux / process (ConPTY на Windows) / docker
  - Agent: claude-code / codex / aider / cursor / opencode / kimicode
  - Workspace: worktree / clone
  - Tracker: github / linear / gitlab
  - SCM: github / gitlab
  - Notifier: desktop / slack / discord / composio / webhook / openclaw
  - Terminal: iterm2 / web
- **Конвенция важнее конфигурации**: hash-based namespace
  (`~/.agent-orchestrator/{sha256(configPath).slice(0,12)}-{projectId}/`),
  всё runtime — в едином `~/.agent-orchestrator/`.
- **Reactions** — событийная модель: `ci-failed`, `changes-requested`,
  `approved-and-green` → автоматическое действие
  (`send-to-agent` / `notify` / `auto-merge`) с `retries` и
  `escalateAfter`.
- **Multi-project** support через единый config.
- **Dashboard-first**: kanban (6 колонок: Working / Ready / Respond /
  Review / Done / Error), web-based, design system «Warm Terminal»
  (JetBrains Mono + Geist Sans, 0px border-radius, brown-tinted dark).
- **3,288 test cases**, выделенная design-документация (DESIGN.md ~310
  строк по типографике/spacing/motion/accessibility/component anatomy).
- **macOS-specific**: `caffeinate` для prevention of idle sleep при
  remote access через Tailscale.

## 3. Сводная таблица возможностей

Легенда: ✅ — есть и хорошо · 🟡 — частично · ❌ — нет.

| Аспект                                  |               orchX               |               OpenHands                |               Ruflo                |                   AO                    |
| --------------------------------------- | :-------------------------------: | :------------------------------------: | :--------------------------------: | :-------------------------------------: |
| **Декомпозиция задач** (LLM-planner)    |    ✅ (FLAT + PHASED + replan)    |           🟡 (через skills)            |          ✅ (GOAP, SPARC)          |      🟡 (через orchestrator agent)      |
| **Параллельные воркеры в git worktree** |                ✅                 |                   🟡                   |          🟡 (в плагинах)           |                   ✅                    |
| **Иерархия фаз с checkpoint'ами**       |                ✅                 |                   ❌                   |       🟡 (hive-mind levels)        |                   ❌                    |
| **Авто-replan на провал**               |                ✅                 |                   ❌                   |   🟡 (adaptive replan via GOAP)    |                   ❌                    |
| **Merge-conflict resolver (LLM)**       |        ✅ (`orchX-merger`)        |                   ❌                   |                 ❌                 |                   ❌                    |
| **3-state verifier reviewer**           | ✅ (confirmed/plausible/refuted)  |                   ❌                   |      🟡 (Byzantine consensus)      |                   ❌                    |
| **Pre-merge review per task**           |                ✅                 |                   ❌                   |                 ❌                 |                   ❌                    |
| **Context compaction**                  |     ✅ (single-pass summary)      |       ✅ (Condenser, native SDK)       |       ✅ (HNSW + retrieval)        |             🟡 (от агента)              |
| **Provider-aware reasoning effort**     |      ✅ (5 семейств моделей)      | ✅ (REASONING_EFFORT_SUPPORTED_MODELS) |        🟡 (3-tier routing)         |          🟡 (per-agent config)          |
| **Bash sandbox + injection-guard**      | ✅ (prefix-extract + quote-aware) |   ✅ (SafeExecutor, in agent-server)   |    ✅ (`@claude-flow/security`)    |        🟡 (через runtime plugin)        |
| **Path-gated edit**                     |                ✅                 |                   ✅                   |                 ✅                 |                   🟡                    |
| **Docker-sandbox runtime**              |                ❌                 |                   ✅                   |         🟡 (через плагины)         |           ✅ (runtime-docker)           |
| **Remote agent server (REST/WS)**       |                ❌                 |                   ✅                   |          🟡 (MCP server)           |           🟡 (web dashboard)            |
| **Долгоживущая векторная память**       |                ❌                 |           🟡 (через Skills)            |        ✅ (AgentDB + HNSW)         |                   ❌                    |
| **Cross-session learning**              |                ❌                 |                   🟡                   |     ✅ (SONA + ReasoningBank)      |                   ❌                    |
| **Agent federation между машинами**     |                ❌                 |            🟡 (Enterprise)             |   ✅ (mTLS + ed25519 + PII gate)   |                   ❌                    |
| **Plugin system**                       |                ❌                 |         🟡 (MCP + microagents)         |          ✅ (33+ plugins)          |              ✅ (7 slots)               |
| **Web UI / Dashboard**                  |          ❌ (только TUI)          |            ✅ (полноценный)            |          ✅ (web ui beta)          |               ✅ (kanban)               |
| **CI failure reaction** (auto-fix)      |                ❌                 |                   🟡                   |       🟡 (worker `testgaps`)       |         ✅ (built-in reaction)          |
| **PR review comment reaction**          |                ❌                 |                   🟡                   |      🟡 (`pr-manager` agent)       |         ✅ (built-in reaction)          |
| **GitHub / Linear / Jira integrations** |         🟡 (только `gh`)          |                   ✅                   |                 🟡                 |                   ✅                    |
| **Multi-LLM routing**                   |      ❌ (per-role override)       |                   ✅                   |          ✅ (3-tier auto)          |             🟡 (per-agent)              |
| **Browser tool (Playwright)**           |                ❌                 |                   ✅                   |    🟡 (`ruflo-browser` plugin)     |                   ❌                    |
| **MCP server / client**                 |                ❌                 |                   ✅                   |           ✅ (314 tools)           |                   🟡                    |
| **Skill / microagent system**           |      ❌ (только role-prompt)      |        ✅ (microagents + Skill)        |        ✅ (60+ agent types)        |              🟡 (skills/)               |
| **Resume падшего прогона**              |    ✅ (по success-result.json)    |        ✅ (Conversation state)         |        ✅ (session-restore)        |        ✅ (через worktree state)        |
| **Idempotent restart**                  |                ✅                 |                   ✅                   |                 🟡                 |                   ✅                    |
| **Production-ready packaging**          |    🟡 (single Python package)     | ✅ (4-package SDK + npm CLI + Docker)  | ✅ (npm + plugins + IPFS registry) |          ✅ (npm CLI + Docker)          |
| **Design system / UX качество**         |         🟡 (CLI TUI only)         |                   ✅                   |                 🟡                 | ✅ (Warm Terminal, дизайн-документация) |
| **Тесты** (в проекте)                   |          🟡 (~7 модулей)          | ✅ (полное unit/integration покрытие)  |   🟡 (огромный объём, но flaky)    |            ✅ (3,288 cases)             |
| **Размер кодовой базы**                 |             ~12k LOC              |            сотни тысяч LOC             |          сотни тысяч LOC           |              ~100k LOC TS               |

## 4. Глубокий разбор различий по доменам

### 4.1 Планирование и декомпозиция задач

| Свойство                    | orchX                                                                       | OpenHands                                   | Ruflo                                                                           | AO                                           |
| --------------------------- | --------------------------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------- |
| Тип планировщика            | Отдельный LLM-агент (`orchX-planner`)                                       | Внутренний agent loop с decomposition skill | GOAP A\* planner + SPARC методология (5 phases) + анти-drift hierarchical swarm | Orchestrator agent (sам решает декомпозицию) |
| Формат плана                | `plan.json` (JSON-schema): FLAT или PHASED                                  | Stateful conversation history               | State-space actions с pre/post conditions                                       | Free-form (orchestrator-as-LLM)              |
| Phases с checkpoint'ами     | ✅ (merge commit между фазами)                                              | ❌                                          | 🟡 (через consensus rounds)                                                     | ❌                                           |
| Cycle detection / валидация | ✅ (Kahn topological sort + cross-phase deny)                               | n/a                                         | 🟡                                                                              | n/a                                          |
| Авто-replan при провале     | ✅ (через `replan-context.md` + self-heal retry)                            | ❌                                          | 🟡 (replan в GOAP, но без явной phase-stratification)                           | ❌ (просто send-to-agent с logs)             |
| Декларативная acceptance    | ✅ (`command` / `file_exists` / `file_contains` + `CheckOutcome`-категории) | ❌ (тесты пишутся имплицитно)               | 🟡 (workflow templates с проверками)                                            | ❌ (CI = единственная acceptance)            |

**Вывод.** orchX — единственный, у кого есть **формальный декларативный
plan.json + acceptance + auto-replan + checkpoint'ы**. Это его главное
архитектурное преимущество для batch-режима «дай ТЗ — получи PR».
OpenHands и AO рассчитаны на интерактивный диалог; Ruflo использует
GOAP, но без жёсткой фазовой стратификации.

### 4.2 Runtime воркера

| Свойство            | orchX                                     | OpenHands                           | Ruflo                                | AO                               |
| ------------------- | ----------------------------------------- | ----------------------------------- | ------------------------------------ | -------------------------------- |
| Где работает worker | `asyncio` корутина в той же Python-сессии | Local process / Docker / RemoteAPI  | Subprocess (Claude Code / Codex CLI) | tmux pane / ConPTY / Docker      |
| Изоляция            | `git worktree` (отдельная ветка)          | Workspace (полный sandbox в Docker) | Зависит от плагина                   | `git worktree` per session       |
| Параллелизм         | `max_parallel` (asyncio.Semaphore)        | Multi-user через agent-server       | Topology-зависимый (8–100+)          | Без явного предела (тmux-сессии) |
| Crash isolation     | Один воркер не валит другого              | Container-level                     | Subprocess-level                     | tmux-level                       |
| Resume              | По `success-result.json` в integration    | Через persisted Conversation state  | session-restore                      | tmux re-attach                   |

**Вывод.** orchX выигрывает в **lightweight**-нише: один Python-процесс,
никакого Docker, никакого daemon'а. OpenHands и AO дают более robust
изоляцию ценой инфраструктуры. Ruflo шире, но heavier (Node runtime,
плагины, IPFS).

### 4.3 LLM-интеграция и эффективность

| Свойство                 | orchX                                                  | OpenHands                                     | Ruflo                                                                          | AO                                 |
| ------------------------ | ------------------------------------------------------ | --------------------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------- |
| Провайдер                | Любой OpenAI-compatible (`/v1/chat/completions`) Proxy | LiteLLM (300+ моделей, native multi-provider) | Anthropic / OpenAI / Google / Cohere / Ollama (5 провайдеров + 3-tier routing) | От агента (Claude / Codex / Aider) |
| Per-role model override  | ✅ (env: `ORCHX_PLANNER_MODEL` и т.п.)                 | ✅ (per-agent config)                         | ✅ (3-tier auto-routing)                                                       | 🟡                                 |
| Reasoning-effort маппинг | ✅ (5 семейств моделей; per-task override)             | ✅ (`reasoning_effort` для o-series, etc.)    | 🟡 (через model selection)                                                     | n/a                                |
| Стриминг                 | ✅ (агрегация tool_calls по `index`)                   | ✅                                            | ✅                                                                             | ✅                                 |
| Cost tracking            | 🟡 (input/output tokens в WorkerOutcome)               | ✅ (telemetry)                                | ✅ (`ruflo-cost-tracker` plugin, бюджеты)                                      | 🟡                                 |
| Context compaction       | ✅ (single-pass LLM summary при ~75% window)           | ✅ (Condenser, native architecture)           | ✅ (HNSW retrieval — выбирает только relevant patterns)                        | 🟡                                 |
| Cross-session memory     | ❌                                                     | 🟡 (через Skills / persisted history)         | ✅ (AgentDB + HNSW + SONA pattern store)                                       | ❌                                 |
| Cache prompt support     | ❌                                                     | ✅ (`CACHE_PROMPT_SUPPORTED_MODELS`)          | ✅                                                                             | 🟡                                 |

**Вывод.** orchX и OpenHands — на одном уровне по reasoning-effort, но
OpenHands шире по моделям и токен-кэшу. Ruflo — **далеко впереди по
memory/learning** (HNSW + SONA + ReasoningBank), что критично для
долгоживущих систем.

### 4.4 Безопасность

| Свойство                | orchX                                               | OpenHands                                        | Ruflo                                                           | AO                         |
| ----------------------- | --------------------------------------------------- | ------------------------------------------------ | --------------------------------------------------------------- | -------------------------- |
| Bash sandbox            | ✅ (prefix-detection + quote-aware injection-guard) | ✅ (через agent-server isolated env)             | ✅ (`@claude-flow/security`: SafeExecutor, PathValidator)       | 🟡 (от runtime plugin)     |
| Edit path-gating        | ✅ (sandbox в cwd; `permission.edit:` glob-list)    | ✅ (FileEditor только в workspace)               | ✅ (PathValidator против traversal)                             | 🟡                         |
| Container isolation     | ❌                                                  | ✅ (DockerWorkspace по умолчанию для production) | 🟡 (через runtime plugin)                                       | ✅ (runtime-docker plugin) |
| Никогда не пушит в main | ✅ (только integration → PR)                        | n/a                                              | n/a                                                             | n/a                        |
| Anti-SSRF в webfetch    | ✅ (private/loopback IPs блокируются)               | ✅                                               | ✅                                                              | n/a                        |
| Secrets management      | 🟡 (.env, gitignored)                               | ✅ (encrypted storage в agent-server)            | ✅ (federation PII pipeline + AIDefence)                        | 🟡                         |
| Prompt-injection защита | ❌                                                  | 🟡 (Security policy в SDK)                       | ✅ (AIDefence plugin: blocks prompt injection + PII)            | ❌                         |
| Permission system       | ✅ (per-role frontmatter)                           | ✅ (Security policy + action risk)               | ✅ (claims-based authorization)                                 | 🟡                         |
| CVE remediation tooling | ❌                                                  | 🟡 (через agent)                                 | ✅ (`@claude-flow/security`: input validation, hashing, tokens) | 🟡                         |
| Audit log               | 🟡 (Plain logs per task)                            | ✅                                               | ✅ (HIPAA/SOC2/GDPR compliance modes)                           | 🟡                         |

**Вывод.** orchX силен в **очень узкой нише** (bash injection-guard +
edit path-gating). Для production-grade security он сильно отстаёт от
Ruflo (AIDefence, PII gate, federation trust) и OpenHands (Docker
isolation + Security policy).

### 4.5 Reviewer / quality gate

| Свойство                                       | orchX                               | OpenHands              | Ruflo                                       | AO                      |
| ---------------------------------------------- | ----------------------------------- | ---------------------- | ------------------------------------------- | ----------------------- |
| Финальный код-ревью                            | ✅ (3 finder-angle + verifier-pass) | 🟡 (через agent skill) | ✅ (`reviewer` agent + Byzantine consensus) | ❌ (от GitHub-reviewer) |
| 3-state verifier (confirmed/plausible/refuted) | ✅ — снижает noise                  | ❌                     | 🟡 (через consensus voting)                 | ❌                      |
| Pre-merge per-task review                      | ✅ (опциональный flag)              | ❌                     | 🟡 (через workflow templates)               | ❌                      |
| Findings → PR body таблица                     | ✅                                  | ❌                     | 🟡                                          | n/a                     |
| Findings → auto-debugger tasks                 | 🟡 (planned, в TODO)                | ❌                     | 🟡 (через TeammateIdle hook)                | ✅ (через reaction)     |
| Иерархия severity                              | ✅ (blocking / major / minor / nit) | n/a                    | 🟡                                          | n/a                     |

**Вывод.** **Reviewer-pipeline orchX уникален.** 3-state verifier с
автоматическим отсевом refuted findings — серьёзное архитектурное
преимущество, которого нет ни у кого из остальных.

### 4.6 Память, learning, federation

orchX **полностью не имеет** ни долгоживущей памяти, ни cross-session
learning, ни federation. Каждый прогон — изолированный.

- **OpenHands** — Skills и persisted Conversation state дают условную
  «память» в рамках одного пользователя/проекта.
- **Ruflo** — лидер: AgentDB + HNSW (150x–12,500x speedup), SONA neural
  patterns (<0.05ms adaptation), ReasoningBank, EWC++ против
  catastrophic forgetting, Flash Attention.
- **AO** — без cross-session memory, только session metadata.

Для batch-системы вроде orchX это не критично (один прогон = один PR),
но **отсутствие RAG по предыдущим успешным/упавшим прогонам того же
репо** — потенциальная слабость на больших monorepo с тысячами задач.

### 4.7 UI/UX и операционная видимость

| Свойство                      | orchX                          | OpenHands       | Ruflo                         | AO                                        |
| ----------------------------- | ------------------------------ | --------------- | ----------------------------- | ----------------------------------------- |
| Live progress                 | ✅ TUI (tui.py, 756 LOC)       | ✅ Web GUI      | 🟡 (через goal.ruv.io/agents) | ✅ Kanban dashboard (Next.js 15)          |
| Multi-project view            | ❌                             | 🟡              | 🟡 (через namespaces)         | ✅ (config с несколькими projects)        |
| Per-agent логи                | ✅ (отдельный файл на attempt) | ✅              | ✅                            | ✅                                        |
| Cost dashboard                | ❌                             | ✅              | ✅ (`ruflo-cost-tracker`)     | 🟡                                        |
| Notifications (Slack/Discord) | ❌                             | ✅ (Enterprise) | 🟡                            | ✅ (notifier plugin)                      |
| Дизайн-документация           | ❌                             | ✅              | 🟡                            | ✅ (полноценный DESIGN.md, design tokens) |
| Дашборд кросс-репо            | ❌                             | ✅ (Cloud)      | ✅ (federation)               | ✅                                        |

**Вывод.** orchX — **headless-first**. Это сознательный trade-off (CLI
batch-tool), но при росте параллелизма (десятки воркеров, длительные
прогоны) без normalised dashboard'а сложно следить за состоянием —
TUI хорош для одного прогона, но не для многих параллельных.

### 4.8 Workflow и интеграции

| Свойство                           | orchX                             | OpenHands           | Ruflo                          | AO                                               |
| ---------------------------------- | --------------------------------- | ------------------- | ------------------------------ | ------------------------------------------------ |
| GitHub PR creation                 | ✅ (`gh pr create`)               | ✅                  | 🟡 (`pr-manager` agent)        | ✅                                               |
| Linear / Jira tracker              | ❌                                | ✅ (Enterprise)     | 🟡                             | ✅ (tracker-linear plugin)                       |
| Slack / Discord notif              | ❌                                | ✅                  | 🟡 (federation)                | ✅ (notifier plugins)                            |
| CI auto-fix loop                   | ❌ (только после первого прохода) | 🟡 (если запросить) | 🟡 (через worker `testgaps`)   | ✅ (reaction: ci-failed → send-to-agent)         |
| PR review comments reaction        | ❌                                | 🟡                  | 🟡                             | ✅ (reaction: changes-requested → send-to-agent) |
| Auto-merge при approved + green CI | ❌                                | 🟡                  | 🟡                             | ✅ (опциональный auto-merge)                     |
| Multi-repo orchestration           | ❌                                | 🟡                  | ✅ (federation)                | ✅ (config с несколькими projects)               |
| Webhooks / events                  | ❌                                | ✅                  | ✅                             | ✅                                               |
| Browser automation                 | ❌                                | ✅ (Playwright)     | 🟡 (`ruflo-browser` plugin)    | 🟡                                               |
| Database migrations support        | ✅ (через `allow_replan: false`)  | ❌                  | ✅ (`ruflo-migrations` plugin) | ❌                                               |

**Вывод.** AO **доминирует в feedback loop**: PR → CI → review → auto-fix
— это его UX-killer-feature. У orchX этот цикл заканчивается на
«открыли PR» — дальше человек.

### 4.9 Extensibility / plugin architecture

- **orchX** — расширяется через `.orchx/prompts/orchX-<role>.md` (custom
  roles) и `.orchx/PROJECT.md` (контекст проекта). Tool registry —
  hardcoded.
- **OpenHands** — extensible через `Tool`-subclasses + MCP + microagents.
- **Ruflo** — **33+ native plugins + 21 npm plugins + IPFS-registry +
  marketplace** (`/plugin install`). 314 MCP tools. Plugins автоматически
  расширяют возможности.
- **AO** — **7 plugin slots** с чёткими TypeScript-интерфейсами
  (`packages/core/src/types.ts`). Каждый plugin — реализация одного
  интерфейса.

**Вывод.** orchX почти не расширяемый — это compatible trade-off для
batch-режима, но при росте сообщества/use-case'ов отсутствие plugin API
ограничивает.

## 5. Сильные стороны orchX (что делать **не нужно**)

1. **Декларативный plan.json + acceptance-grade.** Никто из 3 проектов
   не имеет такой жёсткой формализации задач — это даёт reproducibility
   и легко auditable PR.
2. **PHASED-планы с checkpoint'ами.** Single best architectural feature
   для миграций БД, рефакторингов, multi-layer изменений.
3. **Auto-replan с self-heal retry.** Если фаза падает — orchX
   автоматически перепланирует остаток, сохраняя оригинальный task_id и
   завершённые фазы.
4. **3-state verifier reviewer (confirmed/plausible/refuted).** Снижает
   noise в финальном code review, чего нет ни у кого.
5. **Pre-merge per-task review с эскалацией в debugger.** Уникальная
   фича, ловит correctness-bugs ДО накопления.
6. **Bash injection-guard + prefix-extract sandbox.** Серьёзный shift
   относительно простых allow-list'ов — Ruflo делает это похожим, но
   только в `@claude-flow/security`.
7. **Provider-aware reasoning-effort маппинг с per-task override.**
   Гранулярный контроль стоимости при сохранении качества.
8. **Никаких внешних зависимостей кроме openai-SDK + PyYAML + git + gh.**
   Минимальный footprint для self-hosted CI.
9. **Git worktree-изоляция воркеров.** Это reasonable middle-ground:
   нет docker-overhead'а, но workers не мешают друг другу.
10. **Compaction для длинных воркеров** через single-pass summary —
    решает context-window problem.

## 6. Слабые стороны orchX (что улучшать)

Сгруппированы по убыванию impact'а.

### 6.1 P0 — Архитектурные пробелы

| Пробел                                                  | Кто это уже делает     | Цена для orchX                                                                                 |
| ------------------------------------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------- |
| **Нет feedback loop с PR (CI fails / review comments)** | AO (centerpiece)       | После открытия PR human должен закрывать каждую правку вручную                                 |
| **Нет долгоживущей памяти / RAG**                       | Ruflo (AgentDB + HNSW) | Каждый прогон с нуля — на больших monorepo planner повторяет ошибки прошлых прогонов           |
| **Нет MCP-интеграции**                                  | OpenHands / Ruflo / AO | Не подключаемся к экосистеме external tools                                                    |
| **Нет docker-runtime опции**                            | OpenHands / AO         | Worker bash может тронуть всё внутри cwd; для chaos-experiments или untrusted-кода это рисково |
| **Нет browser-tool (Playwright)**                       | OpenHands              | UI-задачи вынуждены ограничиваться unit-тестами                                                |

### 6.2 P1 — UX/Operations

| Пробел                                        | Кто решает                               | Цена для orchX                                                                  |
| --------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------- |
| **Нет web-dashboard'а**                       | AO / OpenHands                           | На длительных run'ах TUI не справляется; нет multi-run view                     |
| **Нет multi-project orchestration**           | AO / Ruflo (federation)                  | Каждый репо = отдельный CLI invoke                                              |
| **Нет cost dashboard / budget alerts**        | Ruflo (`ruflo-cost-tracker`) / OpenHands | Можно случайно прожечь $$$ на runaway-прогоне (защита только wall-time hardcap) |
| **Нет notifications (Slack/Discord/webhook)** | AO / OpenHands                           | Длительные прогоны требуют tail-ить лог вручную                                 |
| **Plugin system отсутствует**                 | Ruflo (33+) / AO (7 slots)               | Custom integrations требуют forka orchx-исходника                               |

### 6.3 P2 — Качество кода и тестирование

| Пробел                                             | Цена для orchX                                                                         |
| -------------------------------------------------- | -------------------------------------------------------------------------------------- |
| **orchestrator.py — 2671 строк**                   | Сильное cognitive bloat; сложно onboard'ить новых contributors                         |
| **models.py — 679 строк**                          | Можно сегментировать на planning / acceptance / state                                  |
| **Только ~7 тестовых модулей**                     | Низкое покрытие критичных компонентов (compaction, replan, supervisor)                 |
| **Нет integration-тестов** (end-to-end с mock-LLM) | Regression-tests добавляются после bug'а, не до                                        |
| **Нет CI** (видимого в репо)                       | Без auto-checks: lint, типы, тесты — это нестабильный foundation                       |
| **Нет mypy / pyright strict-mode**                 | Pydantic-объектов нет, dataclasses — но без runtime-validation                         |
| **TUI 756 LOC** (`tui.py`)                         | Большой компонент, который ниши работает «сам по себе»; стоит вынести как опциональный |

### 6.4 P3 — Documentation & community

| Пробел                                     | Кто делает лучше                         |
| ------------------------------------------ | ---------------------------------------- |
| **Нет полноценного API reference**         | OpenHands (docs.openhands.dev)           |
| **Нет examples каталога**                  | OpenHands / Ruflo / AO имеют `examples/` |
| **Нет ARCHITECTURE.md / DESIGN.md**        | AO (выделенный design-doc)               |
| **Нет CONTRIBUTING.md**                    | Все остальные                            |
| **Нет CHANGELOG / релиз-notes**            | Все остальные                            |
| **Нет community channels** (Slack/Discord) | Все остальные                            |

## 7. Один параграф «что выгодно копировать у каждого»

- **У OpenHands** — **4-package SDK-архитектура**. Чёткое разделение
  `sdk` (типы), `tools` (реализация), `workspace` (runtime), `server`
  (опциональный HTTP) — это даёт чистые boundaries, типизированный API
  и возможность embed'а orchX в third-party Python-приложения.
  Дополнительно: **Condenser/Skill/Security как built-in concepts** —
  готовые классы, не разбросанная логика.
- **У Ruflo** — **долговременная векторная память (AgentDB + HNSW +
  ReasoningBank)** и **trajectory learning**. Для orchX это бы дало
  «учится на своих ошибках» — после провалов planner мог бы избегать
  повторных грабель.
- **У AO** — **plugin slots (runtime / agent / workspace / tracker /
  scm / notifier / terminal)** и **reactions** (`ci-failed`,
  `changes-requested`, `approved-and-green`). Это превращает batch-tool
  в continuous-feedback loop. Дополнительно: **convention over
  configuration** (hash-based namespace, auto-derived paths) — сильно
  упрощает onboarding.

## 8. Финальный позиционный вердикт

orchX занимает **уникальную нишу**, в которой никто из остальных не
конкурирует напрямую:

- **Standalone batch-планировщик для git-проектов с формальным
  declarative-plan'ом + acceptance + PR-output.**
- Заточен под **headless CI / cron / human-in-the-loop через GitHub
  PR-review**.
- Архитектурные фичи (PHASED + checkpoint + auto-replan + 3-state
  reviewer + merger + pre-merge review) — сильнее, чем у любого из
  трёх остальных в их «git-worktree спавн» части.

Но **операционно** orchX — самый молодой и наименее polished:

- нет долговременной памяти и обучения (vs Ruflo),
- нет feedback-loop с PR / CI / review comments (vs AO),
- нет docker-runtime, browser tool, REST API, plugin system (vs OpenHands).

Дорожная карта (в `docs/recommendations.md`) описывает, **как закрыть
эти пробелы, не пожертвовав уникальностью orchX**: добавить
plugin-slots, memory backend, PR reactions, MCP-bridge, docker-runtime
— но сохранить декларативный plan.json как ядро системы.
