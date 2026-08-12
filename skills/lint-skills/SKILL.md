---
name: lint-skills
description: Lint and validate Polisade Orchestrator skill definitions
---

# /polisade:lint-skills — Validate Skill Definitions

Meta-skill для разработчиков плагина. Проверяет все `skills/*/SKILL.md` на корректность.

## Использование

```
/polisade:lint-skills    # Проверить все skills
```

## Алгоритм

1. Запустить линтер:

```bash
python3 {plugin_root}/scripts/polisade_lint_skills.py {plugin_root}
```

2. Распарсить JSON-ответ.
3. Вывести результат.

## Проверки

- **Frontmatter**: наличие `name`, `description`
- **Heading**: соответствие `/polisade:{name}` в заголовке
- **Algorithm section**: наличие секции Algorithm/Алгоритм
- **Cross-references**: `/polisade:` ссылки указывают на существующие skills
- **Deprecated skills**: помечены в frontmatter

## Формат вывода

```
═══════════════════════════════════════════
Polisade Orchestrator LINT SKILLS
═══════════════════════════════════════════

[PASS] init
[PASS] state
[PASS] feature
[PASS] implement
[PASS] continue
[PASS] review
[PASS] review-pr
[PASS] doctor
[PASS] sync

───────────────────────────────────────────
Checked: 25 skills
Errors: 0, Warnings: 0
═══════════════════════════════════════════
```

## Важно

- **Read-only** — ничего не модифицирует
- Используется для CI и pre-release валидации
