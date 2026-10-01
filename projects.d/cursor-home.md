---
name: cursor-home
remote: https://github.com/olenchikovad1/cursor-settings.git
path_expr: ~/.cursor
default_base_branch: master
branch_prefix: task/
autopush: true
rolling_commits: true
wip_branches: true
---

# Контекст проекта cursor-home

Сам `~/.cursor`: скиллы, библиотека `project_skills`, хуки, разрешения,
эталоны времени, планы и этот реестр проектов. Синхронизируется между домом
и офисом через приватный GitHub, как `claude-home` у Claude.

Работа идёт прямо в `master`. Катящийся коммит сессии — в `wip/<роль>`,
`master` публикуется на конце сессии (`hooks/autosync_cursor.py`).

Не синхронизируются: `local.json`, чаты `projects/`, встроенные скиллы
приложения, кэш, журнал замеров `time-analysis/records`.
