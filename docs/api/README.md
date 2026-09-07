# Локальный API v1

Базовый адрес: `http://127.0.0.1:8000`. В web используется прокси `/api`. OpenAPI: `/docs` и `/openapi.json`. Cookie возвращается при создании гостя; передавайте её в последующих запросах. `guest_session_id` — информационный идентификатор, он не даёт доступ без cookie.

| Метод | Путь | Результат |
| --- | --- | --- |
| GET | `/health/live`, `/health/ready` | 200, готовность локальной базы и режим |
| GET | `/api/v1/config` | Режим, поддержанные UI-локали |
| POST | `/api/v1/sessions/guest` | Сессия и HttpOnly cookie, first-touch attribution |
| POST | `/api/v1/dreams` | 201, dream_id и queued/processing/completed |
| GET | `/api/v1/dreams/{id}/free` | Состояние, stage, результат или error, can_retry |
| POST | `/api/v1/dreams/{id}/free` | 202, повтор failed-задачи; для завершённой/активной ничего не дублирует |
| POST | `/api/v1/dreams/{id}/viewed` | 204, одно событие просмотра готового результата |
| DELETE | `/api/v1/dreams/{id}` | 204, удаление записи/результата/связанных событий |

Пример тела создания сна (заголовок `Idempotency-Key` обязателен: случайный UUID, один на логическую отправку):

```json
{
  "dream_text": "Я увидел знакомый дом и открыл дверь маленьким ключом.",
  "wake_emotion": "любопытство",
  "dream_date": "2026-09-05",
  "recurring": false,
  "locale": "ru-RU",
  "content_language": "ru",
  "recognition_locale": null,
  "region": null,
  "cultural_perspective": null
}
```

Пример состояния:

```json
{
  "dream_id": "uuid",
  "status": "processing",
  "stage": "extracting",
  "result": null,
  "error": null,
  "can_retry": false
}
```

В completed-результате: demo/template_not_ai либо real/model_from_user_text. `research_retrieval_used=false` в обоих режимах. Стартовый health подтверждает локальную готовность/режим, но provider_connection_verified=false: он не делает платную проверку ключа. Текущий frontend не вызывает POST free после создания: создание уже ставит задачу в очередь. Polling — раз в 700 мс, 110 секунд на клиентский цикл. Если клиент не дождался, он может запросить состояние, не отправляя сон повторно.

Коды ошибок: 401 session_required; 404 dream_not_found (включая чужого владельца); 409 idempotency_conflict/retry_exhausted; 422 invalid_input (только названия полей); 429 rate_limited; 503 provider_not_configured. Processing errors: interrupted, timeout, processing_failed. Raw provider exception и raw input не возвращаются. Платёжные, Telegram, diary, admin endpoints исходной спецификации ещё не реализованы.

Реальный адаптер и коды provider_auth/provider_rate_limited/provider_unavailable/provider_refused/invalid_model_output/safety_support/safety_refused описаны в [этапе 02](../stages/02-real-ai.md). Отказ/опасность/ошибка ключа отключают retry; остальные ошибки допускают явную повторную попытку до общего лимита.
