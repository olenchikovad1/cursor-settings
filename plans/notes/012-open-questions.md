# 012 — открытые вопросы владельцу

## US-0008. База прослойки на sandbox — завести по SQL (нужен владелец)

Решение 01.10.2026: база прослойки — в системном Postgres sandbox
(`127.0.0.1:5432`, рядом с витриной `docroi`), не в DataLake. Мой доступ туда
без пароля не проходит, роли заводит владелец. Hub доходит до неё по уже
поднятому туннелю `docroi-tunnel:5432` — новых портов и правок sshd нет.

Два пароля придумать и положить в установку (`local/secrets/docroi/`):
`<пароль_layer>` — роль записи для прослойки, `<пароль_ro>` — роль чтения
для приложения на hub. В git они не попадают.

**Подключение** (на sandbox, под учёткой с правом создавать базы):

```bash
ssh -p 22 olenchikov.a@161.104.50.20
# Postgres службой хоста — через сокет суперпользователем:
sudo -u postgres psql -p 5432
# Если он в контейнере (я с sandbox этого не вижу — нет пароля postgres):
# docker exec -it <контейнер postgres витрины> psql -U postgres
```

**Что выполнить** (вставить целиком, подставив пароли):

```sql
-- Роль записи: ею пишет только прослойка на sandbox.
create role docroilayer login password '<пароль_layer>';
-- Роль чтения: ею приложение на hub читает витрину-прослойку (И-4).
create role docroilayer_ro login password '<пароль_ro>';

create database docroilayer owner docroilayer;
\connect docroilayer
-- Чужим ролям в базу хода нет; читателю — только чтение и только таблиц,
-- которые прослойка заведёт потом (default privileges — от владельца).
revoke all on database docroilayer from public;
grant connect on database docroilayer to docroilayer_ro;
grant usage on schema public to docroilayer_ro;
alter default privileges for role docroilayer in schema public
  grant select on tables to docroilayer_ro;
alter role docroilayer_ro set default_transaction_read_only = on;
```

**Что считается успехом:** `psql -h 127.0.0.1 -p 5432 -U docroilayer -d docroilayer -c 'select 1'`
отвечает `1`; `psql ... -U docroilayer_ro -d docroilayer -c 'create table t()'`
отвечает отказом (`cannot execute CREATE TABLE in a read-only transaction`).

После этого прослойку на sandbox поднимаю я (разрешение «серверы: all» получено):
миграции `alembic_layer`, первый прогон кусками, демонстрация обрыва и
продолжения — на настоящих данных витрины и озера.

Следующая прослойка получает там же свою базу и свою пару ролей тем же SQL —
таблицы этой не делит, озеро не трогает.

## US-0008. Кусок — строго новее отметки: оговорка

Отметка «забрано до» ставится на последний день куска; следующий прогон
читает дни **строго новее**. Следствие: день, который в витрине дозаписан
после того, как его забрали (финотчёт WB доезжает с опозданием), в прослойку
сам не приедет — то же, что и записано в плане («перезапись закрытого дня
эта история не делает»). Если так случится, лечится сдвигом отметки назад
руками: `update load_mark set loaded_until = '<день>' where dataset = 'sales_day'`.
Неделя остатков — исключение: её неделя перечитывается целиком, иначе
биты наличия за дни до отметки терялись бы.

## US-0008 / US-0010. Деструктивная миграция приложения на hub (З-6)

Ревизия `e8cd35922b8d` сносит из базы приложения на hub таблицы фактов
(номенклатура, продажи, остатки, недели, заказы, отметки) **с данными**.
Решение владельца 01.10.2026 — «drop». Применится при первой выкатке
`develop` через installation после слияния плана. До слияния — если это надо
отложить, сказать.

## Проверки типов (mypy) — красные до меня

`mypy docroiapi docroilayer` в тестовом контейнере даёт 22 ошибки, все в
коде приложения до плана 012 (`change_log.py`, `repositories/docroi.py`,
`documents_bucket.py`, устаревшие `type: ignore` в выгрузках). К плану не
относятся, в проверки перед слиянием mypy не входит. Чинить отдельно?
