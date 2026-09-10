---
id: DRIFT-WAIVER-NNN
status: active
expires: YYYY-MM-DD
approved_by: "[имя PM / ревьюера, утвердившего waiver]"
created: YYYY-MM-DD
suppresses:
  - "api.missing_in_code:GET /example/{}"
---

<!--
DRIFT-WAIVER — ревьюируемый артефакт, ЕДИНСТВЕННЫЙ легальный способ временно
пропустить красный drift-gate (scripts/polisade_drift_gate.py, issue #205).

Правила класса (замена агентского флага design_waiver):
- Файл живёт в docs/waivers/DRIFT-WAIVER-NNN.md и попадает в PR — его видит
  и утверждает ревьюер/PM. Агент НЕ создаёт waiver самостоятельно.
- `expires` обязателен: после даты гейт снова красный (fail-closed).
- `suppresses` — точные finding keys из отчёта гейта (`--json`), допускаются
  fnmatch-шаблоны (например "er.missing_column:orders.*"). Ключи видны
  в выводе гейта: api.missing_in_code:<METHOD> <path>,
  api.undocumented:<METHOD> <path>, er.missing_table:<table>,
  er.missing_column:<table>.<column>, er.extra_table:<table>.
- `status: active` — единственное действующее значение; для отзыва waiver
  меняй на revoked (гейт перестанет его применять) или удаляй файл.
-->

# DRIFT-WAIVER-NNN: [краткое название дрейфа]

## Обоснование

[Почему дрейф временно допустим: контекст, ссылка на SPEC/TASK/ADR,
почему нельзя устранить сейчас.]

## План устранения

[Что именно и к какому сроку закроет дрейф. Срок должен согласовываться
с `expires` в frontmatter.]
