# Integration Contracts

## Интеграционная карта

| Контракт | Тип | Направление | Формат | Версия | Статус |
|----------|-----|-------------|--------|--------|--------|
| *пример: api-frontend.yaml* | *REST API* | *provided* | *OpenAPI 3.0* | *1.0.0* | *Active* |
| *пример: partner-api.xsd* | *SOAP* | *consumed* | *XSD* | *1.2* | *Active* |

> Удалите примеры и заполните реальными контрактами.

## Структура

```text
docs/contracts/
├── provided/    # Контракты, которые наша система ПРЕДОСТАВЛЯЕТ (мы контролируем)
├── consumed/    # Контракты, которые мы ПОТРЕБЛЯЕМ от смежных систем (read-only)
└── README.md    # Этот файл — интеграционная карта
```

## Правила

### provided/
- Файлы, которые мы создаём и поддерживаем: OpenAPI YAML, AsyncAPI YAML, Protobuf, GraphQL SDL
- Source of truth для code generation (клиенты, валидаторы, документация)
- Обновляются через `/pdlc:design` или вручную

### consumed/
- Файлы, полученные от смежных команд/вендоров: XSD, WSDL, OpenAPI, Protobuf
- **Read-only** — не модифицируются при реализации
- Обновляются только при получении новой версии от поставщика
- Подпапка `examples/` — примеры запросов/ответов для тестирования (WireMock stubs, fixtures)

## Поддерживаемые форматы

| Формат | Расширение | Применение |
|--------|-----------|------------|
| OpenAPI 3.0+ | `.yaml` | REST API |
| AsyncAPI 3.0+ | `.yaml` | Messaging (Kafka, RabbitMQ) |
| XSD | `.xsd` | SOAP / XML services |
| WSDL | `.wsdl` | Legacy SOAP services |
| Protocol Buffers | `.proto` | gRPC |
| GraphQL SDL | `.graphql` | GraphQL |
