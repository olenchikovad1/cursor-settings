# Синхронизация `~/.cursor`

Дом и офис делят один приватный репозиторий
`github.com/olenchikovad1/cursor-settings`. Рабочая копия — сама папка
`~/.cursor`. Роль машины — `home` или `office` в `local.json`, файл в git не
входит.

## Что едет

Скиллы, библиотека `project_skills`, хуки, `permissions.json`, эталоны
`references`, планы, код калибровки.

Не едет: чаты (`projects/`), встроенные скиллы приложения, кэш, `cli-config.json`,
`local.json`, журнал замеров `time-analysis/records`. Журнал меняется каждый
ход; отдельная ветка `calibration`, как у Claude, сюда ещё не перенесена, и
замеры остаются на той машине, где сняты.

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
