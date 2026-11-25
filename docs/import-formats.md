<!-- docs/import-formats.md -->
# Импорт номенклатуры: поддерживаемые форматы и структуры

Этот документ должен быть кратко пересказан в **корневом `README.md`** проекта, а по ссылке «Форматы файлов» быть доступен пользователю в `/admin/import`. Примеры для скачивания: `examples/import_formats/`. Важное правило: ни формат файла, ни совпадение имён/цен не доказывают идентичность Product — все данные проходят единый pipeline этапа 02 v2.

## Общие поля

Обязательное: `name` — непустое наименование. Необязательные: `source_key`, `brand`, `manufacturer`, `mpn`, `unit`, `quantity`, `price`, `currency`, `price_date`, `seller`, `seller_inn`, `product_url`, `category_path`.

- `source_key` — ключ позиции у исходного источника (не обязательно артикул изготовителя).
- `price`, `quantity` — десятичные числа **в текстовом представлении** с точкой для канонического контракта, например `"129.50"`; ошибки разбора сохраняются в Raw и issues. Не использовать binary float для денежных сумм.
- `currency` — код ISO 4217, при отсутствии не угадывать.
- `price_date` — `YYYY-MM-DD`; отсутствие не означает сегодняшнюю дату источника.
- `seller`/`seller_inn` — организация продавца, **не бренд** и не изготовитель.
- `category_path` — подсказка от источника, не UUID канонической категории.

### JSON (`catalog_items_v1`)

```json
{
  "schema_version": 1,
  "source": "user_upload",
  "items": [
    {
      "source_key": "row-001",
      "name": "Выключатель концевой XCKN2145P20",
      "brand": "Telemecanique Sensors",
      "manufacturer": null,
      "mpn": "XCKN2145P20",
      "unit": "шт",
      "quantity": "2",
      "price": "129.50",
      "currency": "RUB",
      "price_date": "2024-11-18",
      "seller": "Пример продавца",
      "seller_inn": null,
      "product_url": null,
      "category_path": ["Электрика", "Выключатели"]
    }
  ]
}
```

Эти значения иллюстративны: цена, дата и продавец **не относятся к реальной продаже XCKN2145P20**. Строгая схема: `docs/contracts/catalog_items_v1.schema.json`.

### JSON (`aitim_archive_v1`, исторический профиль)

Также **обязательно принимать исходный AITIM** без преобразования:

```json
{
  "source": "aitim.online / КАЦ / Архив цен",
  "total_unique_records": 1,
  "completed_queries": ["кабель"],
  "records": [{
    "name": "Кабель для примера",
    "unit": null,
    "price": "65",
    "url": "https://example.org/product/1",
    "date": "30.11.2024",
    "organization": "ООО Пример",
    "inn": null,
    "matched_queries": ["кабель"]
  }]
}
```

Поля AITIM `date` формата `DD.MM.YYYY` преобразовать в `price_date`; `url` → `product_url`; `organization` → `seller`; `inn` → `seller_inn`; `matched_queries` сохранять без потерь в Raw; `brand` и `mpn` не выдумывать. `total_unique_records` проверять как диагностическую метрику, но не считать доказательством фактического числа валидных Product.

### XML (`catalog_items_v1`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<catalog schema_version="1" source="user_upload">
  <item>
    <source_key>row-001</source_key>
    <name>Выключатель концевой XCKN2145P20</name>
    <brand>Telemecanique Sensors</brand>
    <mpn>XCKN2145P20</mpn>
    <unit>шт</unit>
    <quantity>2</quantity>
    <price>129.50</price>
    <currency>RUB</currency>
    <price_date>2024-11-18</price_date>
    <category_path><part>Электрика</part><part>Выключатели</part></category_path>
  </item>
</catalog>
```

Схема `docs/contracts/catalog_items_v1.xsd`; DTD/внешние сущности запрещены.

### XLSX

Первая строка выбранного листа — заголовки, каждая следующая строка — отдельная позиция. Минимум колонка `name` (или явное сопоставление другой колонки с `name`). Для нескольких листов пользователь выбирает, какие обрабатывать. Пример файла `examples/import_formats/sample_items.xlsx`.

| source_key | name | brand | mpn | unit | quantity | price | currency | price_date | seller | category_path |
|---|---|---|---|---|---|---|---|---|---|---|
| row-001 | Выключатель концевой XCKN2145P20 | Telemecanique Sensors | XCKN2145P20 | шт | 2 | 129.50 | RUB | 2024-11-18 | Пример продавца | Электрика > Выключатели |

`price_date` может храниться как ISO-строка либо Excel date; в preview показать фактически распознанное значение. Не выполнять формулы; не заполнять отсутствующую единицу измерения догадкой. Не поддерживаем `.xls` и `.xlsm` в этом этапе.

### CSV (не SVC)

Формат называется **CSV**, расширение `.csv`. Кодировка UTF-8, разделитель по умолчанию `;`, обязательный header:

```csv
source_key;name;brand;mpn;unit;quantity;price;currency;price_date;seller;category_path
row-001;"Выключатель концевой XCKN2145P20";"Telemecanique Sensors";XCKN2145P20;шт;2;129.50;RUB;2024-11-18;"Пример продавца";"Электрика > Выключатели"
```

CSV поддерживает `;`, `,`, табуляцию после предпросмотра; если заголовки другие, пользователь сопоставляет их UI. Не интерпретировать значения с формульным префиксом как команды Excel.

## Неоднозначности и ошибки

Если нет `name`, строка попадает в отчёт с `invalid_record`. При `unit=null` — `missing_unit`, при неверном цене — `invalid_price`; `seller` не означает `manufacturer`; при неизвестном `mpn` не объединять товары по похожему имени. Актуальность цены **не проверяем** на этом этапе. При массовой загрузке ошибка отдельной строки не уничтожает остальные.

## Что ожидать после автозапуска или нажатия «Начать обработку»

1. `RawRecord` сохраняет исходную запись.
2. `NormalizedRecord` отражает результат нормализации и issues.
3. Fast lookup ищет только по доказанным идентификаторам.
4. Для нового/неясного изделия VLM получает `answer_1` согласно утверждённому текстовому промпту.
5. Product matching выполняется ещё раз с проверкой характеристик.
6. SQL rules/aliases → Qdrant candidate search → VLM `answer_2` (при необходимости).
7. SQL проверяет актуальность категории и статус назначения.
8. Категория подтверждается или позиция попадает в очередь проверки.
9. Историческая входная цена сохраняется при достаточных данных, но НЕ объявляется текущей.

При недоступности GPU/Qdrant обработка не теряет исходную запись: пользователь получает статус ошибки инфраструктуры/нужна проверка и возможность продолжить после восстановления сервиса.
