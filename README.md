# Telegram Bot + YooKassa

Telegram-бот с оплатой через YooKassa, сбором данных покупателя и админ-выгрузкой покупок.

## Возможности

- Команды `/start`, `/help`, `/admin`
- Кнопки:
  - `Купить доступ`
  - `Проверить оплату`
  - `Помощь`
  - `Админ панель` (только для админов)
- Перед созданием платежа пользователь вводит:
  - ФИО
  - адрес
  - номер телефона
- Создание платежей в YooKassa с `Idempotence-Key`
- Проверка статуса платежа
- Webhook-обработка событий YooKassa
- Выгрузка всех покупок в CSV из админ-панели

## Подготовка

1. Скопируйте `.env.example` в `.env` и заполните значения.
2. Установите зависимости:

```powershell
py -m pip install aiogram==3.27.0 aiohttp==3.13.5 aiohttp-socks==0.10.1 pydantic-settings==2.12.0 SQLAlchemy==2.0.44 aiosqlite==0.21.0 pytest==8.4.2 pytest-asyncio==1.2.0
```

## Запуск

```powershell
py -m app
```

Health check webhook-сервера:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/health
```

## Тесты

```powershell
py -m pytest -q
```

## Обязательные переменные окружения

- `TELEGRAM_BOT_TOKEN`
- `YOOKASSA_SHOP_ID`
- `YOOKASSA_SECRET_KEY`
- `ADMIN_TELEGRAM_IDS`

