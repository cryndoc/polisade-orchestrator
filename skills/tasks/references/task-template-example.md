# Структура TASK файла: пример

> Вынесено из skills/tasks/SKILL.md (issue #134, progressive disclosure).

```markdown
---
id: TASK-001
title: "Создать сервис экспорта PDF"
status: ready
created: 2026-02-02
parent: FEAT-001
priority: P1
depends_on: []
blocks: [TASK-003, TASK-004]
requirements: [SPEC-001.FR-001]   # composite ID из parent SPEC/PRD/FEAT секций 5/6
design_refs: []          # пути внутри DESIGN-PKG (если у parent SPEC есть design package)
---

# Задача: Создать сервис экспорта PDF

## Контекст

**Parent:** [[FEAT-001]]

**Зачем:** Пользователи хотят экспортировать отчёты в PDF для печати и sharing.

## Что нужно сделать

1. [ ] Создать `src/services/pdf-export.ts`
2. [ ] Реализовать функцию `exportToPdf(data: ReportData): Promise<Buffer>`
3. [ ] Использовать библиотеку jsPDF
4. [ ] Добавить форматирование таблиц
5. [ ] Добавить header/footer

## Файлы для изменения

- `src/services/pdf-export.ts` — создать новый файл
- `src/services/index.ts` — добавить экспорт
- `package.json` — добавить jsPDF dependency

## Критерии приёмки

- [ ] Функция возвращает валидный PDF buffer
- [ ] Таблицы корректно форматируются
- [ ] Русский текст отображается правильно
- [ ] Unit тесты покрывают основные сценарии

## Edge cases

- Пустые данные
- Очень большие таблицы (100+ строк)
- Спецсимволы в тексте

## Тесты

### Unit тесты
- [ ] `exportToPdf` с пустыми данными
- [ ] `exportToPdf` с большой таблицей
- [ ] Форматирование дат и чисел
```
