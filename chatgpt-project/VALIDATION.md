# Проверка локального пакета и контракта

Актуальный статус 2026-09-07: [проект Sonnic](https://chatgpt.com/g/g-p-6a9c47e07510819197bd898c90d1b12b-sonnic-baza-znanii-i-razbor-snov/project) существует, закреплён и подключён к живому HTTPS MCP. Синтетический вызов ChatGPT прошёл claim, submit, review и completed. Сайт теперь развёрнут в owner-only personal beta: произвольный DreamInput, явное согласие, owner-bound доступ и логическое удаление проверены. Проектные файлы v1.1 ещё нужно обновить перед первым личным вызовом. Обычный ChatGPT Plus требует одного сообщения владельца для запуска каждого job.

Дата: 2026-09-07. Среда: Windows, Python из `.venv`. Ниже фактические результаты этого этапа.

| Проверка | Результат |
| --- | --- |
| `scripts/build_chatgpt_package.py` | Успешно: 11 allowlisted файлов, 12 источников |
| `python -m pytest -q --basetemp=.cache/pytest --tb=short` | **76 passed** |
| `npm run test:e2e` | **52 passed**: desktop + mobile |
| `npm --workspace apps/web run build` | production build passed |
| HTTPS owner beta smoke test | personal create → queued → cancel; payload scrubbed |
| `scripts/check_chatgpt_bridge.py` | **queued → leased → awaiting_review → completed**, `.cache/bridge-preview.json` создан |
| SHA256 исходного DOCX | Совпадает: `16FE214DE00DF12B5AAFD051D25C84B5329B1DACE6FEF4AB79281F91944D55DC` |

В pytest два прежних предупреждения устаревания Starlette/httpx и anyio; падений нет. Тесты bridge проверяют области доступа и чужого владельца, фиксированный синтетический набор, конфликты идемпотентности, lease/общий срок, максимум попыток, отмену и поздний ответ, правильность хеша/языка/evidence, обязательный review, публичную проекцию без служебных данных, конкурентный claim и сохранность после открытия базы заново. Проверки пакета сверяют SHA256, размеры, исходный каталог, JSON Schema и согласованность пары примеров.

CLI использует вручную написанный ответ и тестовое одобрение. Он не вызывает ChatGPT, не публикует результат на сайте и не доказывает качество модели. Одобрение здесь проверяет только механику перехода в completed. Сетевая аутентификация, MCP, доступ проекта к файлам, retrieval, автоматический запуск и изоляция личных снов не проверены.

История предыдущей проверки, до успешной загрузки и живого теста: браузерный шаг был остановлен до чтения вкладок: auto-review отклонил `cua.getState` из-за лимита. Сообщение пользователя об открытом ChatGPT сохранено; вход не проверен инструментом. Проект, инструкции, загрузки и закрепление не объявляются выполненными. Обходов не выполнялось.

Frontend не менялся: typecheck/build и 38 браузерных сценариев этапа 02 остаются историческими успешными проверками, в этом этапе не запускались. Настоящий микрофон по-прежнему не проверен.

Для повторения из корня Sonnic:

```powershell
.venv\Scripts\python.exe scripts/build_chatgpt_package.py
.venv\Scripts\python.exe -m pytest -q --basetemp=.cache/pytest --tb=short
.venv\Scripts\python.exe scripts/check_chatgpt_bridge.py
```

Повторять после изменения контрактов/набора/пакета или появления нового сбоя; без причины заново запускать уже прошедшие проверки не требуется.
