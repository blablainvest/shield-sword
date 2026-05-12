# Щит и Меч

Локальный dashboard для поиска высоковолатильных движений на Bybit USDT perpetuals.

Проект сейчас решает две задачи:

- **Сканер**: показывает топ роста и топ падения за выбранный период от 1 до 24 часов.
- **Пайплайн**: запускает ручное исследование выбранной монеты и сохраняет карточку анализа.
- **Бэктесты**: журнал уже созданных research-карточек для будущей проверки сигналов.

Система работает read-only: реальные ордера не создаются.

## Запуск

```bash
PYTHONPATH=src python3 -m hype_radar serve --host 127.0.0.1 --port 8765
```

Открыть dashboard:

```text
http://127.0.0.1:8765
```

## Настройки

Скопируй пример окружения:

```bash
cp .env.example .env
```

Основные переменные:

```text
BYBIT_BASE_URL=https://api.bybit.com
COINGECKO_API_KEY=
LUNARCRUSH_API_KEY=
OPENAI_API_KEY=
OPENAI_TRANSLATION_MODEL=gpt-4.1-nano
```

Bybit public market data работает без ключей. CoinGecko, LunarCrush и OpenAI нужны для более глубокого исследования карточек.

## Что анализируется

Сканер использует Bybit:

- изменение цены за выбранный период;
- изменение объема за выбранный период;
- 24h liquidity filter;
- funding;
- open interest;
- long/short ratio.

Research-карточка использует:

- CoinGecko для описания проекта, сектора, метрик MC/FDV/circulation;
- LunarCrush для соцтем, активности и кратких социальных тезисов;
- Bybit для рыночного контекста, манипулятивности, фандинга, OI и базового технического наброска.

CoinGecko в текущей схеме дает базовый паспорт проекта, а не инсайты. На одно research-исследование может уходить до пяти запросов:

- `search`: сопоставляет Bybit ticker с CoinGecko coin id;
- `coin-data`: достает описание, категории, platforms, links и supply;
- `coins-markets`: достает MC, FDV, объем и рыночные метрики;
- `trending`: глобальный список трендовых монет, кандидат на кеш 6-24ч;
- `categories`: глобальная динамика категорий, кандидат на кеш 6-24ч.

Nansen, DEX liquidity, on-chain holders и блеклисты сейчас не используются.

## CLI

Разовый скан из терминала:

```bash
PYTHONPATH=src python3 -m hype_radar scan --top 5 --window-hours 24 --format text
```

JSON:

```bash
PYTHONPATH=src python3 -m hype_radar scan --top 5 --window-hours 3 --format json
```

## Проверка

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
node --check src/hype_radar/web/app.js
```

## Данные

Локальная SQLite-история хранится в `data/` и не коммитится.

Секреты хранятся только в `.env`; файл также не коммитится.
