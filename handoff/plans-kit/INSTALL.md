# Установка набора. Читать агенту

Эту папку передали архивом. Задача — разложить её содержимое в домашний
каталог Cursor на этой машине и проверить, что оно работает. Человеку ничего
собирать не надо: все пути, команды и проверка уже здесь.

## Куда ставить

Домашний каталог Cursor — `Path.home() / ".cursor"`. В Windows это
`C:\Users\<имя>\.cursor`. Дальше он называется «дом».

Скиллы, хуки, эталоны и журнал живут **только в доме**. Не класть их в
`.cursor/` открытого проекта и не создавать новый репозиторий.

Python на этой машине запускается как `py -X utf8`. Если `py` не находится —
остановиться и сказать человеку поставить Python 3.11+ с python.org.

## Что куда копировать

Из корня этой папки (там, где лежат `skills/`, `hooks/`, `INSTALL.md`):

| Откуда в наборе | Куда в доме |
|---|---|
| `skills/` | `~/.cursor/skills/` |
| `hooks/` (вместе с `hooks/tests/`) | `~/.cursor/hooks/` |
| `references/` | `~/.cursor/references/` |
| `time-analysis/` | `~/.cursor/time-analysis/` |
| `plans/` | `~/.cursor/plans/` |
| `hooks.json` | см. ниже — это не копия файла целиком |

Копировать, а не переносить: папка набора остаётся на месте. `__pycache__`,
`README.md` и `INSTALL.md` в дом не копировать.

## Если файл уже есть

- Файла нет — записать.
- Файл есть и совпадает — пропустить.
- Файл есть и отличается — **не затирать**. Назвать путь и спросить человека.
  У него там может быть своя правка.

Исключение одно: `hooks.json`. Его не заменяют, а дополняют.

## hooks.json — дополнить, не заменить

Если `~/.cursor/hooks.json` нет — записать `hooks.json` из набора как есть.

Если есть — в каждый ключ из таблицы дописать команду набора, которой там ещё
нет. Остальные ключи и команды не трогать и не удалять.

| Ключ | Команда | Дополнительно |
|---|---|---|
| `sessionStart` | `py -X utf8 ./hooks/time_estimate.py sessionStart` | `"timeout": 20` |
| `beforeSubmitPrompt` | `py -X utf8 ./hooks/time_estimate.py beforeSubmitPrompt` | `"timeout": 20` |
| `afterAgentResponse` | `py -X utf8 ./hooks/time_estimate.py afterAgentResponse` | `"timeout": 10` |
| `postToolUse` | `py -X utf8 ./hooks/time_estimate.py postToolUse` | `"timeout": 10` |
| `postToolUse` | `py -X utf8 ./hooks/lint_plan.py --hook` | `"timeout": 15`, `"matcher": "Write\|StrReplace\|EditNotebook\|Shell"` |
| `stop` | `py -X utf8 ./hooks/time_estimate.py stop` | `"timeout": 20`, `"loop_limit": 1` |

Пути в командах начинаются с `./hooks/` — так и оставить: пользовательские
хуки Cursor запускает из `~/.cursor`. После правки файл должен остаться
валидным JSON (проверить `py -X utf8 -m json.tool ~/.cursor/hooks.json`).

## Проверка

Запустить все три команды и прочитать вывод целиком.

```powershell
py -X utf8 ~/.cursor/hooks/tests/test_kit_hooks.py
py -X utf8 -m unittest discover -s ~/.cursor/time-analysis -p "test_*.py"
py -X utf8 ~/.cursor/time-analysis/calibrate.py estimate --sp 3 --compared-to US-9007 "проверка"
```

Готово, когда одновременно:

- первые две команды заканчиваются на `OK`;
- третья печатает JSON с полем `"minutes"` (около 21 — норма для стартовых полок);
- на месте `~/.cursor/skills/write-plan/SKILL.md` и
  `~/.cursor/references/stories/AXES.md`.

Если команда падает — показать вывод человеку и остановиться, не чинить набор
по догадке.

В конце сказать человеку: перезапустить Cursor, чтобы он подхватил хуки, а в
новом чате попросить «составь план» — сработает скилл `write-plan`.

## Чего не делать

- Не вызывать `python` вместо `py -X utf8`.
- Не удалять стартовый `time-analysis/records/steps.jsonl`: это версия 1
  эталонов, без неё памятка шагов пустая.
- Не править тексты скиллов и код при установке.
