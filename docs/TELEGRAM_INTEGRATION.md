# Подключение Telegram-интерфейса к backend

Этот документ описывает Python API `BackendService` и контракт Telegram-адаптера.
Реализация на `aiogram` находится в `app/bot`. Запуск описан в README.

Источники истины: `app/core/service.py`, `app/shared/models.py`,
`app/shared/ports.py`, `app/shared/errors.py`. При расхождении сначала согласуйте
изменение с участником №2; не добавляйте второй backend.

## 1. Граница ответственности

Бот принимает Telegram updates, определяет действительного пользователя, показывает
результат, собирает явное согласование и вызывает методы сервиса. Backend хранит
бизнесы, источники, изображения, версии, задания, согласования и результаты
публикации. Scheduler запускает подготовку, публикацию, уведомления и сбор метрик.

Из обработчика бота запрещено:

- напрямую менять SQLite, `service.repo`, `service.files` или модели состояния;
- вызывать `Publisher.publish`, отправлять контент в канал через `sendPhoto`;
- создавать собственный scheduler или повторно запускать backend;
- превращать ответ AI или подтверждение контент-плана в согласование поста;
- автоматически повторять отправку со статусом `delivery_unknown`.

Отправка предпросмотра и уведомления в личный чат владельца относится к интерфейсу;
отправка согласованного поста в канал относится к backend.

## 2. Фабрика и один экземпляр приложения

Зарезервирован путь `app.bot.adapter:build_bot`. Его создаёт участник №3:

```python
def build_bot(service, settings):
    # Возвращает экземпляр вашего адаптера, не запускает polling здесь.
    return TelegramBotAdapter(service, settings)


class TelegramBotAdapter:
    async def run(self, stop_event):
        # Polling/webhook lifecycle и корректная остановка.
        ...

    async def send(self, notification):
        # Notification -> личный чат notification.owner_id.
        ...
```

Runtime
вызывает фабрику один раз, передаёт существующий сервис и запускает адаптер рядом
с scheduler. Адаптер использует этот же сервис. В фабрике нельзя создавать
`BackendService`, `Repository`, `FileStore` или второй процесс приложения.

Поле настройки: `BOT_FACTORY=app.bot.adapter:build_bot`. В `Settings` оно называется
`bot_factory`. Токен доступен как `settings.telegram_bot_token` (`SecretStr`),
разворачивать его можно только непосредственно для настройки клиента. Не выводите
его, полный URL Telegram-запроса или сырые provider responses в логи.

Версия `aiogram` закреплена в `requirements.txt`.

В `APP_MODE=demo` нельзя запускать настоящий polling, вызывать Telegram или реальные
AI/image providers. Demo предназначен для локальной проверки backend. Для проверки
UI используйте фиктивные Telegram events и тестовый транспорт; реальный бот
запускается только в явно настроенном live-режиме.

## 3. Авторизация и контекст интерфейса

Во все пользовательские методы передавайте `actor_id` из подлинного Telegram
`from_user.id`. Нельзя подставлять `settings.owner_id` вместо реального отправителя:
это обойдёт смысл проверки доступа. Backend текущего MVP разрешает управление
только одному владельцу, указанному `OWNER_TELEGRAM_ID`.

В UI храните выбранные `business_id`, `post_id`, ожидаемую версию поста и вид
ожидаемого ответа. В callback передавайте только идентификатор операции, поста и
**версию, показанную пользователю**. Цена, статус, права и текст из callback не
являются доверенными данными. Не используйте имя пользователя как owner ID.

Для диалогов управления предпочтителен личный чат с ботом. При возвращении после
перезапуска заново получите бизнесы и посты из сервиса. Если контекст потерян,
предложите выбрать объект; не применяйте правку к случайному последнему посту.

## 4. Создание бизнеса и загрузка материалов

Методы без `async` вызываются без `await`. Все даты с временем должны содержать
часовой пояс; хранение и сравнение backend выполняет в UTC.

```python
async def onboard(service, actor_id, name, website, pdf_filename, pdf_bytes):
    business = service.create_business(actor_id, name, timezone="Europe/Moscow")
    service.add_url_source(actor_id, business.id, website)
    service.add_pdf_source(actor_id, business.id, pdf_filename, pdf_bytes)
    business = await service.analyze_business(actor_id, business.id)
    return business  # Показать business.profile и business.profile_version.
```

Файл из Telegram скачивает бот в `bytes`; хранение выполняет `add_pdf_source`.
Не передавайте локальный путь в AI. Проверяйте размер до полной загрузки, когда
метаданные Telegram его содержат; backend также применяет свои ограничения.

Текст владельца можно добавить отдельно:

```python
source = service.add_text_source(actor_id, business_id, user_text, name="О бизнесе")
sources = service.list_sources(actor_id, business_id)
```

`add_url_source` регистрирует URL, но сам ничего не скачивает. Анализ страницы и
документа принадлежит AI-модулю. Не показывайте сообщение «сайт изучен» до успешного
завершения `analyze_business`.

Карточку нужно подтвердить отдельно:

```python
business = service.confirm_profile(actor_id, business_id, expected_version=shown_profile_version)
```

Ручное исправление карточки: создайте валидный `BusinessProfile`, вызовите
`save_profile(actor_id, business_id, expected_version, profile)`, покажите
возвращённую карточку и новый `profile_version`, затем получите новое подтверждение.
Объекты контрактов неизменяемы; не присваивайте поля внутри полученной модели.

Подключение канала:

```python
business = await service.connect_channel(actor_id, business_id, "@your_channel")
```

Live publisher проверяет, что это канал, бот — администратор с правом публикации,
владелец — создатель или администратор. В бизнес сохраняется числовой `channel_id`.
Другая площадка с активными одобренными/незавершёнными отправками может вызвать
`Conflict`; UI должен показать причину, а не менять базу вручную.

## 5. Обычный язык, очередь и повторные Telegram updates

AI распознаёт намерение, но не исполняет согласование:

```python
intent = await service.interpret_request(
    actor_id, business_id, user_text, post_id=explicitly_selected_post_id
)
```

Возможные `intent.action`: `prepare`, `revise_text`, `revise_image`, `report`,
`clarify`. У ответа есть `request`, необязательный `post_id`, `questions`.
Действий `approve` и `publish` в AI-контракте нет. Для правки нужен явно выбранный
пост; для отчёта UI отдельно собирает период. Если намерение `clarify`, покажите
вопросы и дождитесь ответа. Не угадывайте идентификатор поста.

Подготовка ставится в очередь синхронным методом и немедленно возвращает `Job`:

```python
job = service.request_preparation(
    actor_id,
    business_id,
    request=user_text,
    request_id=f"telegram_update:{telegram_update_id}",
)
```

Используйте стабильный ID исходного события. Повторная доставка того же update
должна использовать ту же строку, включая после перезапуска. Не генерируйте UUID
при каждом вызове. Для callback можно использовать `telegram_callback:{callback_id}`;
выберите один источник идентификатора для одного действия и не меняйте его при
повторе. `request_id` должен быть непустым и не длиннее 200 символов.

Backend сам добавляет бизнес к ключу дедупликации. Повтор уже принятого действия
возвращает то же задание, а не запускает дополнительную платную генерацию. Явное
желание пользователя подготовить ещё один набор — новое событие и новый ID.

После получения Job покажите «Задание принято» и сохраните `job.id` в контексте UI.
Не обещайте, что материалы уже готовы. Не вызывайте `prepare_next_job()` из handler:
это работа scheduler.

Получение результатов:

```python
job = service.get_job(actor_id, job_id)
plan = service.get_plan(actor_id, job_id)
posts = service.get_job_posts(actor_id, job_id) if job.status == "done" else []
```

- `pending` / `running`: подготовка продолжается; обновляйте статус ограниченно.
- `done`: покажите план и доступные готовые посты.
- `failed`: покажите `job.error` и предложите новый явный запрос.

План может существовать до завершения всего задания, в том числе после последующей
ошибки генерации изображения. `plan is not None` не означает `job.status == "done"`.
`get_job_posts(actor_id, job_id)` возвращает посты конкретного задания; у PostView
есть необязательный `job_id`. Для общего списка используйте
`list_posts(actor_id, business_id)`: он возвращает **все посты бизнеса**.
Не связывайте строки плана и посты по индексу — отображайте сами посты по их ID.

## 6. Предпросмотр, версия, время и согласование

```python
post = service.get_post(actor_id, post_id)
asset, image_bytes = service.read_asset(actor_id, post.image_asset_id)
```

Покажите `post.text`, изображение, `post.channel_id`, время в часовом поясе бизнеса,
`post.revision`, статус и отметку demo при `post.is_demo`. MIME картинки —
`asset.mime_type`. Caption выводите как простой текст без интерпретации HTML/Markdown.
Более подробные сведения о расписании и версии удобно отправлять отдельным
служебным сообщением, чтобы не превышать лимит подписи к изображению.

Если времени нет, сначала запросите его и создайте новую версию:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

local_time = datetime.fromisoformat(user_iso_datetime)
if local_time.tzinfo is None:
    local_time = local_time.replace(tzinfo=ZoneInfo(business.timezone))
post = service.edit_post(
    actor_id,
    post_id,
    expected_revision=shown_revision,
    scheduled_at=local_time,
)
# Перерисовать предпросмотр по возвращённому post и дождаться нажатия согласования.
```

Для регионов с переходом на летнее время UI должен уточнять неоднозначное/несуществующее
местное время; пример выше рассчитан на согласованный timezone без такого перехода
(например Europe/Moscow). Не подставляйте локальную timezone компьютера разработчика.

Callback согласования вызывает только:

```python
approved = service.approve_post(actor_id, post_id, expected_revision=revision_from_clicked_preview)
```

Не подменяйте `revision_from_clicked_preview` свежей версией из `get_post`: иначе
старая кнопка сможет согласовать невиденные пользователем изменения. При `Conflict`
получите актуальный пост, покажите его и запросите новое явное согласование.

Согласование требует будущего `scheduled_at`. Статус `approved` означает постановку
согласованной версии в расписание, а не факт публикации. Никакого собственного
таймера отправки в боте не нужно. Факт публикации подтверждается только статусом
`published` и результатом backend.

## 7. Правки и отклонение

```python
updated = await service.revise_text(
    actor_id,
    post_id,
    expected_revision=shown_revision,
    feedback="Сделай короче и убери эмодзи",
)
```

```python
updated = await service.revise_image(
    actor_id,
    post_id,
    expected_revision=shown_revision,
    feedback="Светлый фон, чашка на деревянном столе",
)
```

Для ручной замены текста используйте
`edit_post(actor_id, post_id, expected_revision, text=new_text)`.
Изменение текста, изображения, времени или канала создаёт новую revision и отменяет
согласование. После любого изменения показывайте новый предпросмотр. Метода загрузки
произвольной пользовательской картинки пока нет: не обходите это прямой записью в
хранилище, согласуйте расширение с участником №2.

Отклонение:

```python
rejected = service.reject_post(actor_id, post_id, expected_revision=shown_revision)
```

Опубликованный, отправляющийся или имеющий неизвестный результат пост нельзя
редактировать как обычный черновик. Не реализуйте автоматическую повторную
публикацию через кнопку «Изменить».

## 8. Неизвестный результат отправки

При `delivery_unknown` бот должен просить владельца проверить канал. Отправка могла
пройти, а ответ Telegram — потеряться. Самостоятельный повтор запрещён.

Только после отдельного ручного подтверждения владельца:

```python
# Владелец проверил канал и явно подтвердил отсутствие публикации.
resolved = service.resolve_unknown_delivery(
    actor_id,
    post_id,
    expected_revision=shown_revision,
    confirmed_not_sent=True,
)
```

Либо, если владелец нашёл сообщение и указал его положительный числовой ID:

```python
resolved = service.resolve_unknown_delivery(
    actor_id,
    post_id,
    expected_revision=shown_revision,
    message_id=confirmed_telegram_message_id,
)
```

Это два взаимоисключающих варианта. Первый переводит пост в `failed`; для новой
попытки нужны новая версия с будущим временем и новое согласование. Второй фиксирует
опубликованное сообщение. Не вызывайте этот API на основании ответа AI.

## 9. Автоматизация, отчёты и реакции

```python
business = service.configure_automation(
    actor_id,
    business_id,
    enabled=True,
    request="Подготовь один полезный пост о завтраках",
    interval_hours=24,
    first_run_at=first_run_with_timezone,
)
```

Автоматизация создаёт задания и черновики; согласование каждого окончательного
материала остаётся обязательным. Для выключения передайте `enabled=False`.

Отчёт:

```python
report_text = service.build_report(actor_id, business_id, start_aware, end_aware)
# Если ReportProvider ещё не подключён, доступны структурированные данные:
report_input = service.report_data(actor_id, business_id, start_aware, end_aware)
```

Период: `start < end`, не более 366 дней. Не превращайте отсутствие reporter в
придуманный текст с цифрами; покажите доступные данные или понятный статус.

Обработчик подлинного Telegram update с агрегатами реакций:

```python
recorded = service.record_reactions(
    channel_id=str(telegram_chat_id),
    message_id=telegram_message_id,
    total=sum_of_current_reaction_counts,
    observed_at=telegram_event_time_aware,
    event_id=f"telegram_update:{telegram_update_id}",
)
```

`total` — текущее суммарное число реакций, не приращение. Не складывайте повторные
снимки. `observed_at` берите из события, а не из момента повторной обработки.
`False` означает, что событие не записано (например, нет соответствующего
опубликованного поста или это дубль). Метод не имеет `actor_id`: это доверенная
граница интеграции, а не пользовательская команда. При webhook проверяйте секрет
Telegram; не принимайте похожий JSON из обычного сообщения пользователя.

## 10. Уведомления, ошибки и проверки

`send(notification)` отправляет сообщение только `notification.owner_id`, используя
`notification.text`. `notification.post_id` можно использовать для кнопки открытия
поста, `business_id` — для меню бизнеса. Публикация в канал здесь запрещена.
Отправка уведомлений имеет семантику «как минимум один раз»: сбой после отправки
может привести к повтору. Backend владеет outbox; не добавляйте собственный
бесконечный цикл повторов. У уведомлений подготовки есть необязательный
`notification.job_id`: используйте его с `get_job`, `get_plan`, `get_job_posts`.
У прочих уведомлений поле может быть None; тогда используйте бизнес/пост для навигации.

| Ошибка | Поведение интерфейса |
| --- | --- |
| `Forbidden` | Отказать в доступе, не показывать чужие данные |
| `NotFound` | Предложить заново выбрать бизнес или пост |
| `Conflict` | Обновить карточку/предпросмотр, дождаться нового действия |
| `InvalidInput` | Объяснить требование и предложить исправить ввод |
| `ProviderUnavailable` | Сообщить о недоступности; не заявлять успех |

Обрабатывайте `AppError` безопасным сообщением. Не показывайте сырой traceback,
HTTP exception, токен, ответ AI-провайдера. Непредвиденную ошибку логируйте по классу
и безопасному ID операции. Не повторяйте пользовательское действие автоматически,
если оно может создать новую платную генерацию.

Перед PR проверьте: обычный пользователь не управляет приложением; старый callback
не согласовывает новую версию; повтор update не создаёт второе задание; правка
сбрасывает согласование; отсутствие времени не приводит к отправке; status
`approved` не показывается как `published`; demo не обращается к Telegram;
`delivery_unknown` требует ручного разбора; остановка завершает polling и ресурсы.

Порядок создания ветки и PR описан в `docs/ROLE_HANDOFF.md`.
