# 018 — открытые вопросы

## Запись в боевую `plmapi` на hub (US-0458)

Код слияния дублей единиц готов (`scripts.merge_uom_duplicates`).
На hub: cm×7, mm×7, g/m2×2, m×2. Нужно явное «да» на запись в боевую
базу — без него скрипт на hub не запускать.

Команда после разрешения:

```bash
cd /opt/platform-apps/plm
docker compose exec -T plm-api python -m scripts.merge_uom_duplicates
```

Пока: фильтр `active` в options уже скрывает погашенные; слияние на стенде
можно прогнать отдельно.
