---
name: assemblage-point
remote: https://github.com/waist42/assemblage-point.git
default_base_branch: develop
branch_prefix: task/
backend_test_command: cd backend && pytest
frontend_test_command: null
frontend_build_command: cd web && npm run build
frontend_lint_command: cd web && npm run lint
---

# Контекст проекта assemblage-point

Стек: FastAPI + Alembic + Postgres (backend), Vite + React (web), Docker Compose
(отдельные боевой/дев файлы). Бэкенд гоняется в двух вариантах тестов — мокнутые
API-тесты и repo-level тесты на реальном Postgres (см. скилл `backend-tests` в
самом репозитории).

**Фронтенд:** тест-раннер не настроен (`web/package.json` без `"test"` в
`scripts`) — исполняемые тесты для фронтенд-задач писать не на чем. До тех пор
для фронтенд-изменений проверяется `npm run build` + `npm run lint` вместо
тестов. Это не эквивалент TDD, а временный суррогат, и называть его тестами
нельзя.

Ветки/коммиты — см. скилл `git-workflow` в самом репозитории (коммиты на
русском, `<тип>: <summary>`, push только по явному разрешению).

**Ворктри не используются.** Работа идёт в ветке основного рабочего дерева:
`git checkout -b task/<slug> <base>`. Решение пользователя — лучше один
тщательный план на несколько часов, чем параллельные процессы, которые
конфликтуют и жгут токены. См. память проекта
`feedback_no_worktrees_use_assigned_branch.md`.

## Путь на этой машине

Путь **не хранится здесь** — он машинный и живёт в `local.json` (в
`.gitignore`). Разрешается через `hooks/resolve_project.py` по совпадению
`remote` с `git remote get-url origin`.

Известные пути (для справки, не для использования кодом):

- `DESKTOP-FA9EUCC` (дом): `D:/Проекты/Laretto/assemblage-point`
- `RBB-0001` (офис): `C:/Projects/assemblage-point` — проверено на месте
  03.09.2026, лежит в кэше `local.json`, резолвер отдаёт его без пересканирования
