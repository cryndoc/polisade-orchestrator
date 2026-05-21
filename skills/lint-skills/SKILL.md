---
name: lint-skills
description: Lint and validate Polisade Orchestrator skill definitions
---

# /pdlc:lint-skills — Validate Skill Definitions

Meta-skill для разработчиков плагина. Проверяет все `skills/*/SKILL.md` на корректность.

## Использование

```
/pdlc:lint-skills    # Проверить все skills
```

## Алгоритм

1. Запустить линтер:

```bash
python3 {plugin_root}/scripts/pdlc_lint_skills.py {plugin_root}
```

2. Распарсить JSON-ответ.
3. Вывести результат.

## Проверки

- **Frontmatter**: наличие `name`, `description`
- **Heading**: соответствие `/pdlc:{name}` в заголовке
- **Algorithm section**: наличие секции Algorithm/Алгоритм
- **Cross-references**: `/pdlc:` ссылки указывают на существующие skills
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
[WARN] codex-review — Skill is deprecated
[WARN] codex-review-pr — Skill is deprecated
[PASS] doctor
[PASS] sync

───────────────────────────────────────────
Checked: 18 skills
Errors: 0, Warnings: 2
═══════════════════════════════════════════
```

## Важно

- **Read-only** — ничего не модифицирует
- Используется для CI и pre-release валидации
