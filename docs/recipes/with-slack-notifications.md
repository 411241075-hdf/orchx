# Recipe — Slack notifications

> Получать пинг в Slack-канал при ключевых событиях прогона.

## 1. Slack: создать incoming webhook

[Slack docs](https://api.slack.com/messaging/webhooks):

1. Откройте https://api.slack.com/apps → Create New App → From scratch.
2. Включите *Incoming Webhooks*.
3. *Add New Webhook to Workspace* → выберите канал → скопируйте URL
   (`https://hooks.slack.com/services/T.../B.../...`).

## 2. orchX config

`.orchx/.env`:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
```

`.orchx/config.yaml`:

```yaml
notifiers: [slack]

plugin_config:
  slack:
    webhook_url: ${SLACK_WEBHOOK_URL}
    username: orchX
    channel: "#dev-bots"  # опционально
```

## 3. Запустить

```bash
orchx all "Add OAuth login"
```

Получаемые события:

* `:rocket: orchX [run_started]` task_id, phases, tasks
* `:white_check_mark: orchX [phase_completed]` phase_id, duration
* `:pencil: orchX [pr_opened]` URL
* `:warning: orchX [cost_alert]` 75% бюджета
* `:checkered_flag: orchX [run_finished]` counts, total_cost_usd

## Несколько каналов

Можно подключить несколько notifiers — все события улетят в каждый:

```yaml
notifiers: [slack, discord, webhook]
plugin_config:
  slack:
    webhook_url: ${SLACK_WEBHOOK_URL}
  discord:
    webhook_url: ${DISCORD_WEBHOOK_URL}
  webhook:
    url: https://internal-monitoring.example.com/orchx
    auth_token: ${MONITORING_TOKEN}
```
