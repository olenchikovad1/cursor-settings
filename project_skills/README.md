# Библиотека скиллов для проектов (Cursor)

Общие правила разработки, которые ставятся в проекты **выборочно**. Отсюда
Cursor ничего не читает сам: это не `~/.cursor/skills`, а склад. В работу
скилл попадает только после установки в проект — в
`<проект>/.cursor/skills/<область>-<имя>/`.

Личные скиллы (`~/.cursor/skills`) — другое: они действуют во всех проектах и в
проекты не ставятся. Если у проектного скилла есть личный тёзка, в проекте
действует проектная копия — она адаптирована под проект.

Перенесено из `~/.claude/projects_skills`; правила те же, текст поправлен там,
где он говорил про устройство Claude Code.

## Как ставить

В проекте попросить агента: «подтяни скиллы», «поставь скиллы про фронтенд» —
сработает личный скилл `install-project-skills`. Он сам вызывает установщик:

    py -X utf8 ~/.cursor/project_skills/install.py --list
    py -X utf8 ~/.cursor/project_skills/install.py <проект> --area frontend,quality
    py -X utf8 ~/.cursor/project_skills/install.py <проект> --area frontend,quality --apply

Без `--apply` — только план. Существующую копию установщик **не заменяет**:
совпадающую пропускает, отличающуюся перечисляет с различиями — сливает агент,
сохраняя нюансы проекта и раздел «Отступления в этом проекте».

## Устройство

    project_skills/
      skills/<область>/<имя>/SKILL.md   индекс: шапка + правило или оглавление
      skills/<область>/<имя>/*.md       листья пакета, без шапки
      hooks/rails.py                    диспетчер: один процесс на событие
      hooks/_rails_common.py            общее для проверок
      hooks/<проверка>.py               проверки, каждая держит свой скилл
      hooks/tests/                      тесты проверок
      install.py                        установка в проект
      validate.py                       целостность библиотеки

Шапка индекса: `name` (= `<область>-<имя>`, Cursor требует совпадения с
каталогом), `description` блоком `>-` (в одну строку с «: » Cursor читает
описание как пустое), `source`, `hooks` — какие проверки держат правило.
Cursor видит только описание; тело индекса читается, когда скилл выбран, а
листья — только по ссылке из индекса.

После правки — проверка:

    py -X utf8 ~/.cursor/project_skills/validate.py
    py -X utf8 -m unittest discover -s ~/.cursor/project_skills/hooks/tests

## Каталог

| Скилл | О чём | Хуки |
|---|---|---|
| `api-contracts` | контракт эндпоинта целиком, совместимость, идемпотентность | |
| `api-external-calls` | внешние вызовы: таймауты, повторы, отказ, чужие токены | |
| `architecture-configuration` | настройки одним типизированным объектом, проверка на старте | |
| `architecture-dependencies` | библиотека как решение с ценой, версии, обновления | |
| `architecture-placement` | куда класть код: слои, домены, границы, поиск готового | |
| `architecture-untrusted-codebase` | новое в коде, которому нельзя доверять | |
| `data-access` | один путь к данным, сессии, владелец транзакции | |
| `data-schema` | инварианты в базе, типы значений, миграции | |
| `data-storage-choice` | выбор хранилища по правилам, цена новой системы | |
| `docs-context-files` | AGENTS.md и подобные: термины, инварианты, маршрут скиллов | |
| `docs-decisions` | записи решений с отвергнутыми вариантами | |
| `docs-naming-and-comments` | имена по смыслу на английском, комментарии «зачем» по-русски | |
| `frontend-ui` | один набор компонентов, токены, состояния, клавиатура, подсказки | `guard_ui_boundary` |
| `ops-background-jobs` | фоновые задачи, переживающие перезапуск | |
| `ops-containers` | Dockerfile и compose | |
| `ops-logging` | что и как логировать | |
| `ops-release` | выкатка, откат, разбор отказа | |
| `process-git` | ветки, пуш, коммиты, сообщения | `guard_shared_branch_push`, `push_task_branch`, `guard_commit_trailers` |
| `process-repo-service-files` | .gitignore, .gitattributes, стратегия пуша | |
| `quality-automated-enforcement` | вес проверок: предупреждение по умолчанию | |
| `quality-error-handling` | единая обработка отказов | |
| `quality-refactoring` | рефакторинг к выбранной архитектуре | |
| `quality-review` | сверка с правилами проекта, находки с весом | `require_skills` |
| `quality-root-cause` | причина, а не симптом | |
| `quality-security` | секреты, ввод, авторизация, ошибки | |
| `quality-testing` | тест первым, прогон по риску | |
| `quality-typing` | сигнатуры, отсутствие в типе, проверка типов | |
| `quality-verification` | не заявлять «готово» без прогона | `guard_claim_without_run` |

## Хуки

Хук в Cursor почти ничего не может запретить: после инструмента он только
добавляет текст в контекст беседы. Поэтому вес у проверок такой
(`quality-automated-enforcement`):

| Проверка | Событие | Что делает |
|---|---|---|
| `guard_shared_branch_push` | `beforeShellExecution` | пуш в общую ветку — **спрашивает человека** |
| `guard_commit_trailers` | `beforeShellExecution`, `postToolUse` | подпись в команде коммита — **отказ**; подпись в `HEAD` после коммита — предупреждение |
| `push_task_branch` | `postToolUse` | после коммита в ветке задачи пушит её сам |
| `guard_claim_without_run` | `postToolUse`, `afterAgentResponse`, `stop` | «готово» без прогона в этом ходе — одно напоминание вдогонку |
| `require_skills` | `sessionStart`, `beforeSubmitPrompt`, `postToolUse` | перечень скиллов на старте; правка области без открытого скилла — напоминание |
| `guard_ui_boundary` | `postToolUse` | рукописный блок, чужая библиотека вне набора — предупреждение; `--scan` по всему дереву |

Все проверки вызываются одним диспетчером `rails.py <событие>`: один процесс
на событие, общее состояние беседы (`%TEMP%/cursor-rails/<беседа>.json`),
ответы сливаются по старшинству deny > ask > allow. Сломавшаяся проверка
пишет в stderr и не роняет остальные; команда оболочки всегда получает ответ.

Правка скриптом в оболочке тоже видна: после команды оболочки проверки
смотрят файлы, изменённые с начала сессии.

Подпись Cursor в коммитах (`Co-authored-by: Cursor <cursoragent@cursor.com>`)
добавляет клиент, хук её до выполнения не видит. Выключается в **Settings →
Git & PRs → Attribution**.

## rails.json — одни настройки на все проверки

Ищется `.cursor/rails.json`, затем `.claude/rails.json` — в корне проекта и
выше. Значение ключа **заменяет** значение по умолчанию целиком, а не
дополняется. Нет ключа — действует значение по умолчанию.

| Ключ | По умолчанию | Для чего |
|---|---|---|
| `shared_branches` | `["main", "master", "develop"]` | общие ветки: прямой пуш — только с разрешения |
| `auto_push` | `true` | автопуш ветки задачи после коммита |
| `forbidden_trailers` | `["Co-Authored-By"]` | запрещённые подписи в коммитах |
| `allow_cursor_attribution` | `false` | разрешить подпись Cursor |
| `proof_commands` | pytest, npm test, tsc, dotnet test… | что считается прогоном для «готово» |
| `skill_areas` | области → пути (`frontend` → `web/` …) | какие скиллы напоминать при правке каких путей |
| `frontend_src` | `web/src` | корень фронтенда |
| `ui_set_dir` | `<frontend_src>/components/ui` | где лежит набор компонентов |
| `ui_root_dir` | `<frontend_src>/components` | единственная папка компонентов |
| `component_libraries` | radix, tanstack table, headless, antd, mui, chakra, react-select | что импортируется только внутри набора |

Переменная окружения `CURSOR_NO_AUTO_PUSH=1` выключает автопуш на одну сессию.
