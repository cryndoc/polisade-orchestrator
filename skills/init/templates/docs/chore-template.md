---
id: CHORE-XXX
seed: XXXXXXXX  # 8 символов [a-z][a-z0-9]{7} — `polisade_id.py new-seed`.
                # Постоянная опознавалка: id меняется на номер при первом
                # `/polisade:sync --apply` на транке, seed — никогда, и
                # `polisade_id.py resolve <seed>` читает ссылку, сделанную
                # до нумерации (сообщение коммита, заголовок PR).
title: "[Описание]"
status: ready  # draft | ready | in_progress | review | changes_requested | done | blocked | waiting_pm
created: YYYY-MM-DD
category: config  # config | cleanup | upgrade | docs
task: null  # TASK-XXX (создаётся по умолчанию; --no-task отключает)
---

# Chore: [Описание]

## Category / Категория

<!-- config | cleanup | upgrade | docs -->

## What to Do / Что нужно сделать

<!-- Краткое описание работы -->

## Details / Детали

<!-- Конкретные изменения, если нужно -->

## Files / Файлы

- `path/to/file` — что изменить

## Done Criteria / Критерии готовности

- [ ] Изменения внесены
- [ ] Ничего не сломано
