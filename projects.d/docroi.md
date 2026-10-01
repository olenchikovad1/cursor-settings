---
name: docroi
# «Докрой» — приложение платформы, вынесено из `plm` 30.09.2026: им занимается
# другой человек, и выкладки двух продуктов мешали друг другу.
remote: https://github.com/Laretto-Team/docroi.git
# Основная ветка — `develop`; `main` не трогается (З-2 в AGENTS.md).
default_base_branch: develop
branch_prefix: "task/"
backend_test_command: "docker compose --env-file infra/.env -f infra/compose.yaml --profile test run --rm tests uv run pytest tests/ -q"
frontend_test_command: "cd frontend && npm test"
---

# Контекст проекта docroi

Устройство, инварианты, запреты и маршрут по правилам — в `AGENTS.md`
репозитория; правила разработки — в `.cursor/skills/`. Здесь только то, чего
в репозитории нет.

## Контуры

- **Локальный стенд** — `infra/compose.yaml` + `infra/.env`, вход через шлюз
  локальной платформы. Ломается и пересоздаётся свободно.
- **hub** — только через приложение `installation` (`installation.unit.toml`,
  выкатка по пушу в `develop`). Ручной рычаг `deploy/deploy.sh` — лишь когда
  централизованная сборка не идёт, и только по прямой просьбе владельца.
- **sandbox** — DataLake и витрина `docroi`, только чтение. Соседи
  (`data-lake`, `finflow`, `docroi-golden`, старый `plmportal`) не задеваются.

## Особенности

- `legacy/` — прежний «Докрой», образец, а не код: не правится, не
  запускается, хуки и области скиллов его не видят.
- Секреты стенда лежат в git открыто (допущение Д-1), боевые — в
  `installation` (`local/secrets/docroi/`). Личный `deploy/.env` в git не идёт.
- Трейлеры соавторства в коммитах запрещены.
