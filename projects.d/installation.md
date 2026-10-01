---
name: installation
remote: null
default_base_branch: main
branch_prefix: task/
backend_test_command: null
frontend_test_command: null
frontend_build_command: null
frontend_lint_command: null
---

# Контекст проекта installation

Репозиторий установки: состав платформы и приложений (`installation.toml` —
единица, репозиторий, ветка, машина) и выкатка по нему — из git, а не из
рабочих копий. Кода приложений нет. Remote владелец подключит позже.

Смысл и решения — планы 084 (платформенная часть) и 085 (сам репозиторий).
