# Синхронизация `~/.cursor`

Дом и офис делят один приватный репозиторий
`github.com/olenchikovad1/cursor-settings`. Рабочая копия — сама папка
`~/.cursor`. Роль машины — `home` или `office` в `local.json`, файл в git не
входит.

## Что едет

Скиллы, библиотека `project_skills`, хуки, `permissions.json`, эталоны
`references`, планы, код калибровки.

Не едет в `master`: чаты (`projects/`), встроенные скиллы приложения, кэш,
`cli-config.json`, `local.json`.

Журнал замеров `time-analysis/records/` и `matrix.json` едут **отдельной
веткой `calibration`**: на каждом `stop` — `--out`, на `sessionStart` —
`--in`. Слияние — union строк, без force-push. Руками:

```bash
py -X utf8 ~/.cursor/hooks/sync_calibration.py --status
py -X utf8 ~/.cursor/hooks/sync_calibration.py --in
py -X utf8 ~/.cursor/hooks/sync_calibration.py --out
```

### Приватные проекты (не уезжают с машины)

Как у Claude: папка сессии в `~/.cursor/projects/<slug>` — **junction** в
`~/.my_claude/projects/...`. Реальные файлы лежат вне репозитория
`~/.cursor`, автосинк их не видит. Планы таких проектов — только в
`~/.my_claude/plans/` (каталог указан в машинном `local.json` как
`extra_plans_dirs`, в git не входит). Писать их в `~/.cursor/plans/`
запрещено: эта папка синхронизируется.

Новый приватный slug Cursor: перенести содержимое в `~/.my_claude/...`,
заменить папку junction'ом, до первой сессии с автосинком.

## Как устроены ветки

В течение сессии коммит катится и пушится в `wip/<роль>` с
`--force-with-lease`. Этой веткой владеет одна машина. `master` публикуется
обычным push, когда сессия заканчивается. Вторую машину чужая недоехавшая
ветка не сливается сама: на старте печатается доклад.

```bash
py -X utf8 ~/.cursor/hooks/autosync_cursor.py --status
py -X utf8 ~/.cursor/hooks/autosync_cursor.py --show-wip office
py -X utf8 ~/.cursor/hooks/autosync_cursor.py --adopt-wip office
py -X utf8 ~/.cursor/hooks/autosync_cursor.py --drop-wip office --yes
```

## Вторая машина

Клонировать репозиторий в `~/.cursor` нельзя поверх уже лежащей папки.
На офисе: сохранить локальное, клонировать во временную папку, перенести
содержимое, завести `local.json` с `"role": "office"` по `local.example.json`.

## Проверка

`autosync_cursor.py --status` называет роль и ветку `master`. После хода в
`logs/push.log` есть push в `wip/home` или `wip/office`. В `master` этот коммит
попадает на конце сессии, не на каждом ходе.
