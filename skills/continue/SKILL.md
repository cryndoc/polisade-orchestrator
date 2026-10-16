---
name: continue
description: Autonomous work — find and execute ready tasks
cli_requires: "task_tool, codex_cli"
fallback: self
---

# /polisade:continue — Автономная работа

Найти готовые к работе артефакты и выполнять их автономно.

---

## ⛔ КРИТИЧЕСКИ ВАЖНО: Полный цикл обязателен!

```
┌─────────────────────────────────────────────────────────────┐
│  ⛔ ЗАПРЕЩЕНО ставить status: done если:                    │
│                                                             │
│     ✗ Regression tests НЕ прогнаны                          │
│     ✗ PR НЕ создан                                          │
│     ✗ Review НЕ пройден                                     │
│     ✗ Merge НЕ выполнен                                     │
│                                                             │
│  После написания кода статус: in_progress                   │
│  После создания PR статус: review                           │
│  После merge PR статус: done                                │
└─────────────────────────────────────────────────────────────┘
```

**Каждая TASK, выбранная для реализации, проходит ПОЛНЫЙ ЦИКЛ:**
```
1. IMPLEMENT    → код + unit tests + commit      → статус: in_progress
2. REGRESSION   → запуск ВСЕХ тестов проекта     → исправить если упали
3. CREATE PR    → push + pr-create (polisade_vcs.py)  → статус: review
4. REVIEW LOOP  → ждать/исправлять               → повторять до approve
5. MERGE        → pr-merge --task + delete-branch → статус: done пишет pr-merge (exit 0; иначе waiting_pm)
```

**НЕ ПЕРЕХОДИ к следующей TASK пока текущая не завершена полностью или PM
явно не отменил её с причиной.**
`cancelled` закрывает отменённую работу без PR, а `not_actual` закрывает
отклонённый BUG без исправления. Оба исхода требуют `status_reason` и не
считаются `done`; TASK с `depends_on:` на такой исход ждёт явного решения PM.

---

## Использование

```
/polisade:continue    # Начать автономную работу
```

## Алгоритм

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

0. **Один раз на старте прогона** — какие остановки и гейты
   `/polisade:implement` этот цикл НЕ исполняет (у него своё описание цикла:
   шага приёмки, drift-gate и гейтов проекта в нём нет):
   `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_project_gate.py unenforced --root "${POLISADE_WORK_DIR:-.}"`
   - Поле `warning` непустое → напечатай его PM **дословно** первой строкой
     ответа и **повтори дословно** в «Итогах сессии»: автономный прогон PM
     читает с конца. Строку собрал инструмент — не пересказывай, не сокращай,
     не опускай и не смягчай. Молча уходить в автономный merge при непустом
     `warning` нельзя.
   - Поле `warning` пустое → ничего не печатай: остановок, которых цикл не
     исполнит, нет.
   - Вызов отклонён или не запустился → капсула выше: процитируй отказ и
     STOP — «не знаю» не равно «остановок нет».
   - Ответ `unknown subcommand: unenforced` (код 2) — копия скриптов проекта
     старше команды (вендорённая `.polisade/bin`). Доложи PM дословно:
     «скрипты проекта старше команды — `/polisade:doctor` (проверка
     `scripts_vendor`) назовёт команду обновления» — и STOP.
1. Живой статус задач — одной командой (статусы TASK бери ТОЛЬКО из неё:
   в worktree-режиме статус задачи живёт в дереве её ветки, а корень стоит на
   трунке со старым — корневой `.state/PROJECT_STATE.json` и корневые
   `tasks/*.md` остановку в дереве задачи не видят):
   `env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py tasks-live --project-root .`
   Прочие артефакты (PLAN, SPEC, FEAT, PRD, BUG, DEBT, SPIKE) — из
   `.state/PROJECT_STATE.json`.
2. Прочитай `.state/knowledge.json` для контекста проекта
3. Проверь поле `waiting_pm` команды шага 1:
   - Если не пусто → её строки дословно и "Есть N вопросов к тебе. Запусти /polisade:unblock сначала." STOP
4. Кандидаты TASK — `lists.ready` команды шага 1; прочие — `readyToWork`:
   - Задача из поля `active` команды (in_progress / review / changes_requested
     в своём дереве) — НЕ новый старт: только Уровень 0 (resume) ниже.
   - Если кандидатов нет → "Всё сделано или заблокировано. /polisade:state для деталей."
   - Перед выбором TASK прочитай её `depends_on:` и фактические статусы
     зависимостей из `artifactIndex`. Кандидат доступен только если каждая
     зависимость `done`. `cancelled`, `not_actual`, неизвестный или отсутствующий
     статус не удовлетворяют ребру: не начинай TASK, запиши вопрос PM о
     замене/удалении зависимости, изменении объёма или отмене TASK.
5. Если `workspaceMode: "worktree"`:
   - Дерево из поля `worktrees` команды шага 1 с `state: no_task_change` —
     ветка создана, но её задача ещё не сдвинулась (занята другим агентом):
     сопоставь ветку → родитель TASK по конвенции именования (`chore/CHORE-001-…`
     → `parent: CHORE-001`; `plan/PLAN-XXX-TASK-YYY-…` → TASK-YYY) и задачи
     этого родителя пропусти
   - TASK `ready`, которую команда показала в дереве её ветки (`source:
     worktree` — PM ответил через /polisade:unblock), продолжается в том же
     дереве (шаг 1.7 implement переиспользует его)
6. Выбери задачу по приоритету (см. ниже)
7. **Выполни ПОЛНЫЙ ЦИКЛ для задачи** (см. ниже)
8. Повтори с шага 1

## Полный цикл для TASK (максимальная автономность)

```
┌─────────────────────────────────────────────────────────────┐
│  АВТОНОМНЫЙ ЦИКЛ TASK                                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. IMPLEMENT                                               │
│     ├─ Создать ветку (gitBranching: true)                   │
│     ├─ Реализовать код через субагент                       │
│     ├─ Написать unit тесты                                  │
│     └─ Коммит                                               │
│              │                                              │
│              ▼                                              │
│  2. REGRESSION TEST                                         │
│     ├─ Запустить ВСЕ тесты проекта                          │
│     ├─ Если упали → исправить → коммит → повторить          │
│     └─ Если прошли → продолжить                             │
│              │                                              │
│              ▼                                              │
│  3. CREATE PR                                               │
│     ├─ Push ветки                                           │
│     ├─ Создать Pull Request                                 │
│     └─ TASK → status: review                                │
│              │                                              │
│              ▼                                              │
│  3.5. PRE-CHECK: REVIEWER CLI                              │
│     ├─ Детект ревьюер-CLI через OPS-011 helper                 │
│     │    → reviewer.mode = codex | self | blocked          │
│     └─ mode=blocked → STOP с диагностикой                  │
│              │                                              │
│              ▼                                              │
│  4. QUALITY REVIEW (Independent)                            │
│     ├─ /polisade:review-pr [self] для независимого ревью       │
│     ├─ Ревьюер оценивает diff vs TASK                       │
│     ├─ Если score < 8 → improve → re-review (макс 2 итер.) │
│     └─ Если PASS → merge → delete branch → TASK: done       │
│              │                                              │
│              ▼                                              │
│  5. NEXT TASK                                               │
│     └─ Вернуться к шагу 1 алгоритма                         │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  ПРЕРЫВАНИЕ ЦИКЛА (только при):                             │
│  • waiting_pm — нужно решение PM                            │
│  • blocked — неразрешимая техническая проблема              │
│  • Все задачи завершены                                     │
├─────────────────────────────────────────────────────────────┤
│  НЕ ПРЕРЫВАЙСЯ для:                                         │
│  • Падающих тестов — исправь автоматически                  │
│  • Review замечаний — исправь автоматически                 │
│  • Merge конфликтов — разреши автоматически                 │
└─────────────────────────────────────────────────────────────┘
```

### Детали каждого шага

**1. IMPLEMENT**

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

- Приёмка, drift-gate и гейты проекта (security / API compat / миграции /
  performance) здесь НЕ исполняются — что из этого заведено в проекте, назвал
  шаг 0 алгоритма.
- Создай ветку согласно режиму (PLAN mode или стандартный)
- Статус `in_progress` — сразу после этого и только командой
  `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py task-status --task-id TASK-XXX --status in_progress --branch <expected_branch> --project-root "$WORK_DIR"`:
  она ставит статус в TASK.md дерева задачи и кладёт файл в индекс git —
  переход уедет с коммитом реализации (OPS-010). Корневой TASK.md и
  PROJECT_STATE.json не правь: корень стоит на трунке
- Запусти субагент для реализации
- Субагент пишет код + unit тесты для новой функциональности
- Коммит с сообщением `[TASK-XXX] description`

**2. REGRESSION TEST**
```
while tests_failing:
    run_all_project_tests()
    if failed:
        analyze_failures()
        fix_code()
        commit("[TASK-XXX] Fix test failures")
    else:
        break
```

**3. CREATE PR**
- Push ветки на remote — только `polisade_vcs.py git-push` (Phase C ниже): он сам делает `git push` и сверяет SHA; голый `git push` — нет
- Создать PR через `polisade_vcs.py pr-create` (провайдер выбирается автоматически из settings.vcsProvider)
- Обновить статус TASK → `review` — командой `polisade_vcs.py task-status
  … --status review --pr-url <url>` (полная строка — Phase C, шаг 3): статус
  и `pr_url` в TASK.md дерева задачи, без коммита
  <!-- # OPS-010: правка frontmatter TASK.md + PROJECT_STATE.json task-bucket
  НЕ отдельный commit. Либо бандли в следующий `commit_and_push()`
  (IMPROVE-ветка review-loop), либо — при терминальном STOP через
  review_mode="off"/"blocked" — вынеси в единственный `finalize` commit
  `[TASK-ID] Finalize status: review (PR #N)`. НЕ пиши lastUpdated
  в PROJECT_STATE.json (OPS-010) — поле всегда null. -->
- ⛔ **НЕ пиши `lastUpdated`** в PROJECT_STATE.json — его пишет ТОЛЬКО `scripts/_polisade_state_io.py` из сверки и миграции (`/polisade:sync`, `/polisade:migrate` — команды PM, не шаги этого цикла) (OPS-010). Скилл, пишущий это поле сам, порождает лишний status-only коммит — ровно тот дефект, ради которого контракт видов коммитов и введён.

**4. QUALITY REVIEW И MERGE (автоматически, без вопросов!)**

```
⚠️ ВАЖНО:
- PM не делает code review — это автоматизированный процесс
- НЕ СПРАШИВАЙ "Продолжить?" — делай автоматически!
- Ревью делает Codex CLI (default) или текущий агент (`self`) — независимое мнение!
- GitHub не позволяет approve свой PR — merge напрямую после PASS
```

Алгоритм:

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

0. Pre-check: определить reviewer CLI через единый helper (OPS-011):
   - `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_cli_caps.py detect` → JSON в stdout (`caps`) с `reviewer.mode`; читай его из вывода, не через `caps=$(…)`
   - `review_mode = caps.reviewer.mode` → `"codex"` | `"self"` | `"blocked"` | `"off"` (OPS-017)
   - `reason = caps.reviewer.reason` (может быть пустым)
   - `warning = caps.reviewer.warning` — OPS-007: непустое значение означает, что в PATH нашёлся чужой `codex` и был отбит identity-проверкой. Напечатать `⚠ {warning}` перед ветвлением по `review_mode`, чтобы самозванец не оставался silent в логе.
0a. Если `review_mode == "off"` → STOP: reviewer отключён в `settings.reviewer.mode`. TASK остаётся в `review` с созданным PR; PM делает ревью руками и выполняет merge через `/polisade:pr merge <id>` (или закрывает PR).
0b. Если `review_mode == "blocked"` → STOP. Прочитать `reason` и подсказать соответственно:
   - Если `reason` упоминает «settings …» → проблема в настройках:
     ```
     ═══════════════════════════════════════════
     REVIEWER BLOCKED
     ═══════════════════════════════════════════
     Reason: {reason}

     Проверьте settings.reviewer.mode и settings.reviewer.cli
     в .state/PROJECT_STATE.json — текущее значение конфликтует
     с доступными CLI в окружении.

     TASK остаётся в статусе: review
     ═══════════════════════════════════════════
     ```
   - Иначе (reason не задан или указывает на отсутствие CLI) — показать варианты установки:
     ```
     ═══════════════════════════════════════════
     REVIEWER CLI НЕ НАЙДЕН
     ═══════════════════════════════════════════
     Reason: {reason or "no reviewer CLI available"}

     Quality review требует CLI ревьюера.

     Варианты:
       • Codex CLI: npm install -g @openai/codex ИЛИ brew install openai-codex (документация: https://github.com/openai/codex)
       • Claude Code: https://docs.anthropic.com/claude-code
       • Qwen CLI: документация Qwen

     TASK остаётся в статусе: review
     ═══════════════════════════════════════════
     ```
1. Запусти `/polisade:review-pr` (или `/polisade:review-pr self` если `review_mode == self`) — независимый quality review:
   - Ревьюер оценивает PR diff vs TASK requirements
   - Score 1-10 по критериям (acceptance, полнота, качество, тесты, безопасность)
2. Если score < 8 (IMPROVE):
   - Improvement субагент исправляет код
   - Прогоняет тесты, коммит, push
   - Re-review (макс. 2 итерации)
3. Если score >= 8 (PASS):
   - **Merge** (self-approve блокируется провайдером — merge напрямую)

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->

   - `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-merge N --squash --delete-branch --task {TASK-ID} --project-root "${POLISADE_WORK_DIR:-.}"`
   - exit 0 → `done` записал сам `pr-merge --task` (руками НЕ пиши); `task_status.status` не `done_written` — STOP, покажи PM блок инструмента как есть
   - **Ненулевой exit — мерж НЕ подтверждён** (сервер отклонил мерж — защита
     ветки, конфликт, обязательные проверки, нет прав — или мерж прошёл, а
     удаление ветки отказано; что именно, говорит причина): статус TASK →
     `waiting_pm` с причиной дословно — это уже сделал сам `pr-merge`
     (`--task`): отказ записан в `.state/PROJECT_STATE.json` (`mergeRefusals`),
     задача переведена (`task_status` в JSON) в дереве задачи. Статус руками НЕ пиши;
     `task_status.status` = `not_written` — STOP и назови PM `task_status.reason`.
     Мерж НЕ повторяй (повтор того же PR инструмент отклонит сам, без запроса
     на сервер), `done` НЕ пиши, к следующей задаче НЕ переходи — решает PM.
     Доложи PM блок `merge_report` дословно (последний блок stderr; в нём
     `merge_guard.human_action` дословно и строка «Что делать») — своих
     «Вариантов для PM» не составляй, повтор мержа не советуй;
     `pr-merge-ack` — команда человека, сам её НЕ вызывай.
     `merge_guard.status` не `recorded` / `refused_repeat` — отказ НЕ записан и
     повтор НЕ заблокирован: STOP, назови PM `merge_guard.warning` / `detail`.
   - При exit 0 — **автоматически продолжи** со следующей задачей (не спрашивай!)
4. Если 2 итерации пройдены и score < 8:
   - **STOP** — НЕ мержить, НЕ переходить к следующей задаче
   - Статус TASK → waiting_pm
   - Вывести подробный отчёт и варианты для PM

```python
iterations = 0
while iterations < 2:
    review = run_review(pr_number, review_mode)  # Codex CLI or self
    iterations += 1
    if review.score >= 8:  # PASS
        merge = run('${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-merge N --squash --delete-branch --task {TASK-ID} --project-root "${POLISADE_WORK_DIR:-.}"')
        if merge.exit_code != 0:  # мерж НЕ подтверждён
            # waiting_pm и запись отказа уже сделал сам pr-merge (JSON:
            # task_status, merge_guard) — статус руками НЕ пиши
            report_to_pm(merge.merge_report,  # блок инструмента, дословно
                reason=f"pr-merge: {merge.reason or merge.stderr}")  # дословно
            STOP  # мерж НЕ повторять, done НЕ писать — решает PM
        # done записал сам pr-merge (--task): TASK + индекс дерева
        # задачи и `finalize` commit `[TASK-ID] Finalize status: done (PR #N)`
        # в ветке задачи — JSON task_status. Статус руками НЕ пиши.
        if merge.task_status.status != "done_written":  # PM отменил задачу и т.п.
            report_to_pm(merge.merge_report)  # дословно: причина — в строке «Задача»
            STOP  # к следующей задаче НЕ переходить — решает PM
        break
    else:  # IMPROVE
        run_improvement(review.recommendations)
        run_all_tests()
        # OPS-028: commit_and_push() =
        #   git commit ... && ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py git-push \
        #       --branch <expected_branch> --project-root "$WORK_DIR"
        # На exit=2 (push verification failed) → статус waiting_pm,
        # `waitingForPM` индекса дерева задачи и finalize-коммит `… (PR #N)` —
        # командой `polisade_vcs.py finalize-commit --task-id … --status
        # waiting_pm --pr N` (как в Phase C), не руками; PM — `remote_lines` +
        # `reason` из JSON push дословно. break.
        commit_and_push()
else:
    # Max iterations — STOP, ждём PM
    # OPS-010: терминальный waiting_pm без следующего коммита. Статус,
    # `waitingForPM` индекса дерева задачи и единственный `finalize` commit
    # `[TASK-ID] Finalize status: waiting_pm (PR #N)` — командой (она
    # коммитит в ветке задачи, не на трунке). НЕ пиши lastUpdated.

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->
    run('${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py finalize-commit '
        f'--task-id {task_id} --status waiting_pm --pr {pr_number} '
        f'--branch "{expected_branch}" --project-root "{WORK_DIR}" '
        f'--reason "Review ({review_mode}): score {review.score}/10 after 2 iterations"')
    # PM — строка `line` её ответа; вопрос записан в status_reason TASK
    STOP  # НЕ переходить к следующей задаче!
```

**5. NEXT TASK**
- Вернуться к началу алгоритма
- Выбрать следующую ready задачу

## Приоритет выбора (v2.1)

### Уровень 0: Завершить начатое (ОБЯЗАТЕЛЬНО сначала!)
```
⚠️ НЕ НАЧИНАЙ новую TASK пока есть незавершённые!
```
0. `review` — resume-процедура (OPS-008):

   **Phase A — Resolve workspace** (не полагаемся на env предыдущей сессии):
   1. `expected = compute_expected_branch(TASK)` — детерминировано из TASK
      frontmatter (parent + slug), правила именования — в implement §1.7
   2. Дерево задачи — одной командой из корня проекта, той же, что шаг 1.1
      `/polisade:implement` (сам `git worktree add` / `git checkout` не
      выполняй):

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

      `env ${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py task-worktree --task-id TASK-XXX --branch <expected> --project-root .`
      В worktree-режиме она переиспользует worktree ветки или подключает его
      заново (ATTACH без `-b`, `.state/` без `counters.json`, симлинки
      `.venv` / `node_modules` / `vendor`); в inplace — `git checkout <expected>`
      в корне. exit 0 → `WORK_DIR` = `work_dir` ответа. exit 2 → STOP: блок,
      который команда напечатала (или `reason` и `hint`), — PM дословно;
      ничего не создавай и не откатывай.
   3. OPS-001 assertion: `git -C "$WORK_DIR" rev-parse --abbrev-ref HEAD` —
      вывод сравнить с `<expected>` (без `cd … &&` и без `$(…)`: обе формы
      оболочка с ограничениями GigaCode CLI отклоняет всегда)
      Если не совпало → STOP и доклад PM «OPS-001: <вывод> ≠ <expected>»;
      статус не пиши — дерево не то, а команды статуса пишут только в
      дереве задачи

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

   4. Clean working tree: `git -C "$WORK_DIR" status --porcelain`
      Застейдженные `tasks/TASK-XXX-*.md` и `.state/PROJECT_STATE.json` этой
      задачи (`M ` в первой колонке) — переход статуса, сделанный
      `task-status`; это не грязь, он уедет со следующим коммитом ветки.
      Если кроме них не чисто → `waiting_pm` командой
      `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py finalize-commit --task-id TASK-XXX --status waiting_pm --branch <expected_branch> --project-root "$WORK_DIR" --reason "uncommitted changes in resume workspace"`
      (коммитит только TASK.md, не чужие правки), PM — «uncommitted changes
      in resume workspace» и вывод `status --porcelain`. STOP
   5. Экспортировать: `POLISADE_GIT_BRANCHING`, `POLISADE_EXPECTED_BRANCH`, `POLISADE_WORK_DIR`

   **Phase B — Auto-discover PR:**
   1. Прочитать `pr_url` из TASK frontmatter (source of truth;
      НЕ из PROJECT_STATE.artifacts — его schema не расширяем)
   2. Если `pr_url` заполнен → перейти к Phase D

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

   3. Если пуст → discover:
      `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-list --head <expected_branch> --state OPEN --format json --project-root "${POLISADE_WORK_DIR:-.}"`
      (провайдер выбирается автоматически из settings.vcsProvider; GitHub — через gh, Bitbucket — через REST API)
   4. Если discovery нашёл PR → `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py task-status --task-id TASK-XXX --status review --branch <expected_branch> --pr-url <url> --project-root "$WORK_DIR"` → Phase D

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->

   **Phase C — Create PR** (если Phase B не нашла):
   1. `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py git-push --branch <expected_branch> --set-upstream --task-id TASK-XXX --project-root "$WORK_DIR"`
      — **OPS-028**: verified push (не bare `git push`). Хелпер сверяет
      local SHA с remote SHA через `git ls-remote` и сканирует вывод на
      `remote: fatal` / `remote: ERROR` / `pre-receive hook declined` /
      `value too long for type` / `duplicate key value` / `! [rejected]` /
      `non-fast-forward` / `failed to push`.
      - exit=0 → продолжить (step 2: pr-create)
      - exit=2 → push verification failed: статус **`waiting_pm`**,
        `waitingForPM` в индексе дерева задачи и `finalize`-коммит в ветке
        задачи команда уже сделала сама (`--task-id`, поле `finalize`,
        строка «Состояние:» блока) — не повторяй (finalize-commit после отказа не
        зови: вопрос записан, повтор его не меняет); статус и PROJECT_STATE.json
        руками не правь, сверку не запускай (корень на трунке со старым
        статусом — штатно). Блок, который она напечатала (целиком, от рамки ═══ до рамки: «ЖДЁТ PM: …», «Итог push: …»,
        шаги для человека), — PM как есть; шаги сам не выполняй.
        STOP. **НЕ** `git merge`, **НЕ** `git push origin main`,
        **НЕ** `branch -D`.
   2. `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-create --title "[TASK-XXX] ..." --body-file <PR_BODY_FILE> --head <expected_branch> --project-root "${POLISADE_WORK_DIR:-.}"`
      Вызов идемпотентен: если по `<expected_branch>` уже открыт
      PR — скрипт вернёт ЕГО с `existing: true` и exit 0, дубля не будет.
      Это ровно то, что нужно на resume после падения: повторяй шаг спокойно.
      В отчёте PM различай `existing: true` («PR уже был открыт») и
      `existing: false` («PR создан сейчас»).
   3. При успехе → `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py task-status --task-id TASK-XXX --status review --branch <expected_branch> --pr-url <url> --project-root "$WORK_DIR"` → Phase D

<!-- polisade:exec-denied CAPSULE BEGIN -->
> ⛔ **Если вызов скрипта на этом шаге ты СДЕЛАЛ, и он отклонён или не запустился** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard` / `the tool's default permission is 'deny'`, отказ песочницы, ненулевой exit без вывода) — **STOP**. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Процитируй отказ дословно. НЕ пересказывай по исходнику, что скрипт «сделал бы»; НЕ собирай dry-run вручную; НЕ переходи к apply/push/pr-create.
> НЕ транскрибируй скрипт (прочитать → записать копию в `/tmp` или в проект → запустить копию): копия не байт-идентична — уезжают классы символов в regex, форма возврата функций, пропадают целые функции — и молча меняется набор применённых изменений.
> Отказ инструмента — это отказ, а не результат. Доложи PM дословный текст отказа и назови следствие: без рабочего вызова скрипта этот шаг выполнить нечем. Причину не придумывай — её называет только сам текст отказа.
<!-- polisade:exec-denied CAPSULE END -->

   4. При ошибке (нет `.env` для Bitbucket / токен невалиден / VCS CLI недоступен):
      status → **`waiting_pm`** (НЕ `blocked` — иначе зацикливается).
      Статус, `waitingForPM` индекса дерева задачи и `finalize`-коммит — командой (сама ставит `status:` в TASK.md worktree, переносит задачу в индексе того же дерева и коммитит в ветке задачи; в корне проекта — никогда):
      `${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py finalize-commit --task-id TASK-XXX --status waiting_pm --branch <expected_branch> --project-root "$WORK_DIR" --reason "pr_url_request: автоматическое создание PR не удалось — создайте PR вручную и укажите URL через /polisade:unblock"`.
      Вопрос записан командой (`--reason` → status_reason TASK, его найдёт
      /polisade:unblock); в PROJECT_STATE.json его не пиши. PM — в отчёт:
      `"TASK-XXX: автоматическое создание PR не удалось. Ветка: <expected_branch>. Создайте PR вручную через web UI и запустите /polisade:unblock чтобы указать URL. Для диагностики VCS: /polisade:doctor --vcs."`
      ⛔ **НЕ** `git merge`, **НЕ** `git push origin main`, **НЕ** `branch -D`.
      STOP

   **Phase D — Quality review** (существующая логика без изменений):
   запусти Independent Quality Review, merge после PASS
1. `changes_requested` — исправить замечания code review
2. `in_progress` — доделай начатое

### Уровень 1: Следующая по порядку (в рамках PLAN)
```
Если текущая TASK из PLAN, следующая = первая ready TASK из того же PLAN.
Не прыгай на другие PLAN пока текущий не завершён или не заблокирован.
```
3. `ready` TASK из текущего PLAN (по roadmap_item order)

### Уровень 2: Прямая реализация (если нет активного PLAN)
4. `ready` TASK от BUG — исправь баги (P0 > P1 > P2)
5. `ready` TASK — реализуй задачи
6. `ready` TASK от CHORE — выполни простые задачи

### Уровень 3: Критичный техдолг
7. `ready` TASK от DEBT (P0-P1) — security/performance

### Уровень 4: Исследование
8. `ready` SPIKE — исследуй (следи за timebox)

### Уровень 5: Создание задач (если нет готовых TASK)
9. `ready` PLAN → создай задачи (`/polisade:tasks`)
10. `ready` SPEC → создай задачи (`/polisade:tasks`)
11. `ready` FEAT → создай задачи (`/polisade:tasks`) или spec если сложно

### Уровень 6: Проработка
12. `ready` PRD → создай спецификацию (`/polisade:spec`)

### Уровень 7: Обычный техдолг
13. `ready` TASK от DEBT (P2+) — обычный техдолг

## Логика выбора из нескольких ready

Если несколько артефактов одного типа:
1. Сначала по приоритету: P0 > P1 > P2 > P3
2. Затем по дате создания: старые раньше

## Условия остановки

Останавливайся и сообщай PM когда:

### Требуется решение PM
- Бизнес-выбор (приоритет, скоуп)
- Архитектурный trade-off с последствиями
- Неясные требования

→ Поставь `waiting_pm`, добавь вопрос, продолжи с другими задачами

### Техническая блокировка
- Тесты падают и непонятно почему
- Зависимость недоступна
- Ошибка окружения

→ Поставь `blocked`, продолжи с другими задачами

### Всё готово
- Нет больше `ready` задач
- Все `waiting_pm` или `blocked`

→ Выведи итоги и останови работу

## Формат вывода

### Начало работы
```
═══════════════════════════════════════════
АВТОНОМНАЯ РАБОТА
═══════════════════════════════════════════
{warning из шага 0 — дословно; строки нет, если поле пустое}

Найдено ready: 5 артефактов
• 1 TASK от BUG (P1)
• 3 TASK
• 1 SPIKE

Начинаю полный цикл: TASK-001 (от BUG-001, приоритет P1)
───────────────────────────────────────────
```

### Полный цикл одной задачи
```
───────────────────────────────────────────
[1/5] IMPLEMENT: TASK-001
───────────────────────────────────────────
Ветка: fix/BUG-001-login-error
Worktree: .worktrees/fix__BUG-001-login-error/
Реализация через субагент...
✓ Код написан
✓ Unit тесты добавлены
Коммит: abc123

───────────────────────────────────────────
[2/5] REGRESSION TEST
───────────────────────────────────────────
Запуск всех тестов...
✓ 142/142 тестов прошло (8.5s)

───────────────────────────────────────────
[3/5] CREATE PR
───────────────────────────────────────────
Push: fix/BUG-001-login-error → origin
PR #45 создан: [TASK-001] Fix login error handling
URL: https://github.com/org/repo/pull/45
Статус: review

───────────────────────────────────────────
[4/5] QUALITY REVIEW
───────────────────────────────────────────
Score: 8.4/10 → PASS
✓ Acceptance criteria: 9/10
✓ Полнота: 8/10
✓ Качество: 8/10
✓ Тесты: 9/10
✓ Безопасность: 8/10
Merge: squash and merge
Ветка fix/BUG-001-login-error удалена

───────────────────────────────────────────
[5/5] COMPLETE
───────────────────────────────────────────
✓ TASK-001 → done
✓ BUG-001 → done
Worktree: .worktrees/fix__BUG-001-login-error/ (сохранён)
Cleanup: git worktree remove .worktrees/fix__BUG-001-login-error --force

═══════════════════════════════════════════
Переход к следующей задаче: TASK-002
═══════════════════════════════════════════
```

### При падении тестов (автоисправление)
```
───────────────────────────────────────────
[2/5] REGRESSION TEST
───────────────────────────────────────────
Запуск всех тестов...
✗ 140/142 тестов прошло

Упавшие тесты:
• test_auth_middleware — TypeError
• test_session_handling — AssertionError

Анализ и исправление...
Коммит: def456 "[TASK-001] Fix regression"

Повторный запуск...
✓ 142/142 тестов прошло (9.1s)
```

### При quality review с улучшением
```
───────────────────────────────────────────
[4/5] QUALITY REVIEW
───────────────────────────────────────────
Iteration 1: Score 6.8/10 → IMPROVE
  • Acceptance criteria: 7/10
  • Полнота: 6/10 — пропущен null check в auth.ts
  • Тесты: 6/10 — нет edge case тестов

Improvement субагент исправляет...
  • src/auth.ts:45 — добавлен null check
  • tests/auth.test.ts — добавлены edge case тесты
Коммит: ghi789 "[TASK-001] Address quality review feedback"
Тесты: ✓ passed
Push...

Iteration 2: Score 8.4/10 → PASS
Merge: squash and merge
```

### При блокировке (прерывание)
```
───────────────────────────────────────────
⏸️ TASK-003 требует решения PM
───────────────────────────────────────────
Вопрос: "Какой лимит для rate limiting?"
Статус: waiting_pm
Ветка: plan/PLAN-001-TASK-003-rate-limit (сохранена)

Переход к следующей доступной задаче...
```

### Итоги сессии
```
═══════════════════════════════════════════
ИТОГИ АВТОНОМНОЙ СЕССИИ
═══════════════════════════════════════════
{warning из шага 0 — дословно; строки нет, если поле пустое}

Полных циклов завершено: 4
   ✓ TASK-001 → PR #45 merged
   ✓ TASK-002 → PR #46 merged
   ✓ TASK-004 → PR #47 merged
   ✓ TASK-005 → PR #48 merged

В review: 1
   ⏳ TASK-006 → PR #49 (ожидает review)

Ждут PM: 1
   ⏸️ TASK-003: "Какой лимит для rate limiting?"

Заблокировано: 0

Осталось ready: 2

═══════════════════════════════════════════
ПРИЧИНА ОСТАНОВКИ: waiting_pm
   → /polisade:unblock — ответить на вопрос
   → /polisade:continue — продолжить (пропустит blocked)

Сверка стейта (после merge всех PR):
   /polisade:sync --apply
Cleanup worktrees:
   git worktree list → git worktree remove <path> --force
   git worktree prune
═══════════════════════════════════════════
```

## Git Branching поведение

Проверь `settings.gitBranching` и `settings.workspaceMode` в PROJECT_STATE.json.

При `gitBranching: true`:

### Branch naming

**Для TASK от FEAT/BUG/DEBT/CHORE (стандартный режим):**
- Несколько TASK одного родителя → одна ветка
- `feat/FEAT-XXX-slug`, `fix/BUG-XXX-slug`, `debt/DEBT-XXX-slug`, `chore/CHORE-XXX-slug`

**Для TASK от PLAN (режим плана):**
- Каждая TASK = отдельная ветка
- `plan/PLAN-XXX-TASK-YYY-slug`
- После выполнения **каждой** TASK — сразу PR

**Определение режима:** проверь `parent` в TASK файле:
- `parent: PLAN-XXX` → режим плана
- `parent: FEAT-XXX` / `BUG-XXX` / etc. → стандартный режим

### Worktree mode (workspaceMode: "worktree")

Каждая задача получает изолированный git worktree:

Ветку и дерево делает `polisade_vcs.py task-worktree` — тот же шаг 1.1, что в
`/polisade:implement`, первым исполнимым шагом задачи, до любой правки
и коммита: `.worktrees/<ветка, / → __>/` от трунка, `.state/` без
`counters.json`, симлинки зависимостей, отказ трунку, уже несущему коммит
задачи. Все операции — в `work_dir` её ответа; корень проекта остаётся на
трунке.

**Пропуск занятых задач:** перед выбором следующей задачи проверь `git worktree list --porcelain` — если для TASK уже есть активный worktree (сопоставление по имени ветки → TASK-ID), пропусти задачу.

**Graceful fallback:** если `git worktree add` не проходит → `task-worktree` сам откатывается на `git checkout -b` в корне и говорит об этом в `warning`.

### Inplace mode (workspaceMode: "inplace" или отсутствует)

- `task-worktree` делает `git checkout -b {branch_name}` в корне (прежнее поведение)

### Статус в .md frontmatter

Статус пишут только команды, и только в дереве задачи — TASK `.md`
(committed, source of truth для `/polisade:sync`) и индекс
`.state/PROJECT_STATE.json` того же дерева:

```
code_complete → task-status --status in_progress (без коммита: уедет с коммитом реализации)
create PR     → task-status --status review --pr-url <url> (без коммита)
терминальный  → finalize-commit --status waiting_pm|blocked|review [--pr N]
merge         → done пишет сам pr-merge --task после подтверждённого мержа (шаг 4)
```

`Edit` статуса руками — нет; в корне проекта — никогда.

### Worktree lifecycle

```
create worktree → full cycle (implement → test → PR → review → merge)
  → вывести cleanup инструкцию (worktree НЕ удаляется автоматически)
  → перейти к следующей задаче (новый worktree)
```

**Итоговое сообщение для каждой задачи:**
```
Worktree: {worktree_path} (сохранён для правок по ревью)
Cleanup: git worktree remove {worktree_path} --force && git worktree prune
Сверка: /polisade:sync --apply (из основного репо после merge всех PR)
```

При `gitBranching: false`:
- Коммиты идут в текущую ветку (main)
- Без feature branches

## Важно

- Коммить после каждой завершённой задачи
- Не накапливай много изменений
- При сомнениях — лучше спросить PM (waiting_pm)
- Статусы цикла (`in_progress`, `review`, `waiting_pm`, `blocked`) и их
  движение в индексе PROJECT_STATE.json пишут только команды `task-status` /
  `finalize-commit` (и `pr-merge --task`) — в дереве задачи; руками не
  правь и не синхронизируй
- `/polisade:implement` работает только с TASK
- Следи за timebox для SPIKE

## ⛔ ЗАПРЕЩЕНО спрашивать разрешение на продолжение!

```
НЕ ПИШИ:
  "Продолжить с TASK-039?"
  "Хочешь чтобы я продолжил?"
  "Начать следующую задачу?"

ВМЕСТО ЭТОГО — просто продолжай автоматически!
```

Автономный режим означает:
- После merge PR → сразу следующая задача
- После завершения TASK → сразу следующая задача
- Не жди подтверждения PM для технических операций

**Останавливайся ТОЛЬКО при:**
- `waiting_pm` — бизнес-вопрос к PM
- `blocked` — техническая проблема которую не можешь решить
- Все задачи завершены

## GitHub Self-Approve Limitation

GitHub не позволяет approve свой собственный PR:
```
Error: Review Can not approve your own pull request
```

**Решение:** После успешного Independent Quality Review (score >= 8) делай merge напрямую:

<!-- polisade:push-stop CAPSULE BEGIN -->
> ⛔ **Если вызов `polisade_vcs.py` на этом шаге ты СДЕЛАЛ, и он отклонён** (`Command references protected path` / `Install directory is read-protected` / `Filesystem Guard`) — **STOP до push**: ни `git-push`, ни `pr-create`, ни `pr-merge`, ни `pr-comment` больше не выполняются. Вызова не было — отказа нет: сначала выполни команду; этот блок описывает её вывод и сам ответом не служит.
> Bare `git push` запрещён (push обязан проверяться сравнением remote SHA — OPS-028); самодельные REST/curl-вызовы к Bitbucket/GitHub запрещены; helper не транскрибируется в `/tmp`.
> Доложи PM дословный текст отказа из вывода этого вызова, ветку с локальным коммитом и то, что pr-create не выполнялся, — и заверши рецепт на этом. Готовой фразы отказа здесь нет: её даёт только вывод вызова.
<!-- polisade:push-stop CAPSULE END -->

```bash
${POLISADE_PYTHON:-python3} {plugin_root}/scripts/polisade_vcs.py pr-merge N --squash --delete-branch --task {TASK-ID} --project-root "${POLISADE_WORK_DIR:-.}"
```

exit 0 → TASK `done` — записал сам `pr-merge --task`, руками не пиши;
`task_status.status` не `done_written` — STOP, покажи PM блок инструмента как есть.
**Ненулевой exit — мерж НЕ подтверждён** (отклонён
сервером или прошёл без удаления ветки — что именно, говорит причина): TASK →
`waiting_pm` с причиной дословно — это уже сделал сам `pr-merge` (`--task`;
`task_status` и `merge_guard` в JSON), статус руками НЕ пиши; мерж НЕ
повторяй — повтор инструмент отклонит сам, без запроса на сервер. Доложи PM
блок `merge_report` дословно (в нём `merge_guard.human_action` дословно и
строка «Что делать») — своих «Вариантов для PM» не составляй, повтор мержа не
советуй; `pr-merge-ack` — команда человека, сам её НЕ вызывай. `merge_guard.status` не `recorded` / `refused_repeat` —
отказ НЕ записан и повтор НЕ заблокирован: STOP, назови PM
`merge_guard.warning` / `detail`.

Не approve собственного PR (ни через VCS CLI, ни через REST API) — это не сработает.
