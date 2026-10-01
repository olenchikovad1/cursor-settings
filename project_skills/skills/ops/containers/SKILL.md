---
name: ops-containers
description: >-
  Build images and describe the stack so it comes up from scratch and moves
  between environments — layers ordered so dependencies install before code is
  copied, build context trimmed by its own exclusion list, build tooling
  separated from runtime, no secret left in a layer, non-root user, pinned
  base; services declaring readiness rather than start order, values kept
  outside the file, environment differences as overlays. Use when writing or
  fixing a Dockerfile or compose file, when a build is slow or an image is
  huge, when the stack starts but a service is not actually ready, and when
  moving the stack to another environment.
source: project_skills/skills/ops/containers
---
# Контейнеры

Пакет про два файла, которые всегда правят вместе: **описание сборки образа** и
**описание стека сервисов**. Ошибки в них разные по форме и одинаковые по
последствию — стек не поднимается на чужой машине, а почему, выясняется часами.

## Правила

Каждый пункт действует сам по себе. За причиной, примерами и проверкой
открывается лист.

### Сборка образа → [dockerfile.md](dockerfile.md)

- **Описание сборки лежит рядом с тем, что собирает.**
- **Порядок слоёв — правило, а не привычка:** редко меняющееся выше,
  зависимости ставятся до копирования кода.
- **У сборки свой список исключений,** отдельный от того, что скрыто от
  репозитория.
- **Сборочный инструмент не едет в рабочий образ.**
- **Секрет не попадает в слой.** Удаление файла следующей командой слой не
  очищает.
- **Процесс работает не от суперпользователя.**
- **Версия основы зафиксирована.** Подвижный ярлык означает разное сегодня и
  через месяц.

### Описание стека → [compose.md](compose.md)

- **Сборка называет контекст и файл явно** — умолчание привязано к
  расположению и ломается при переносе.
- **Зависимость — это готовность, а не порядок запуска.** Запущенный процесс и
  процесс, принимающий запросы, — разные состояния.
- **Значения не вписаны в описание:** адреса, ключи и пароли приходят снаружи.
- **Различия сред выражаются наложениями,** а не закомментированными строками.
- **По каждому тому сказано, что он хранит и как получить чистое состояние.**
- **Наружу выставляется не всё** — открытый порт базы удобен ровно до первого
  чужого подключения.

## Смежное

- `ops-release` — выкатка того, что этими файлами собрано, и откат.
- `architecture-configuration` — что из этих значений вообще является
  настройкой.
- `quality-security` — почему секрет в слое и открытый порт стоят в одном ряду.
