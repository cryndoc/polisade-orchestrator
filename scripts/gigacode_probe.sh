#!/usr/bin/env bash
# gigacode_probe.sh — read-only capability probe for the GigaCode CLI.
#
# Запусти на корп-машине, где `gigacode` установлен и аутентифицирован.
# Скрипт создаёт ОДИН отчёт `gigacode_probe_<UTC-timestamp>.txt` в текущей
# директории. Перенеси этот файл на локальную машину — по нему мы приблизим
# настройки локального qwen CLI к поведению gigacode.
#
# Что собирается (всё read-only):
#   1. which/version/help — авторитетный источник поддерживаемых флагов.
#   2. help для подкоманд mcp / extensions / config (если они есть).
#   3. GIGACODE_* / QWEN_* env-переменные.
#   4. Листинг (без содержимого файлов) конфиг-дирректорий:
#        ~/.gigacode, ~/.config/gigacode, ~/.qwen, ~/.config/qwen
#      и их `extensions/` поддиректорий.
#   5. Живой capability-пробник: одна non-interactive команда, просит
#      модель перечислить доступные ей инструменты (Task, WebFetch, MCP,
#      run_shell_command, Read/Write/Edit/Grep), модель и контекст.
#      Запуск идёт дважды: с `--allowed-tools=run_shell_command -p` и без,
#      чтобы зафиксировать разницу.
#
# Безопасность:
#   - `umask 077` — отчёт доступен только владельцу.
#   - Нет записи за пределы текущей директории.
#   - Sed-redaction маскирует типичные токен-паттерны в выводе.
#   - Скрипт НЕ читает auth-файлы, только перечисляет имена.
#   - Сетевые вызовы инициирует только сам gigacode во время пробника.
#
# Usage:
#   bash gigacode_probe.sh
#   # или:
#   bash gigacode_probe.sh --no-live    # пропустить два пробника (офлайн)
#
# Перед отправкой отчёта ОБЯЗАТЕЛЬНО просмотри его глазами: автоматическая
# redaction не покрывает все возможные форматы секретов.

set -u
umask 077

LIVE=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-live) LIVE=0; shift ;;
        -h|--help) sed -n '1,38p' "$0"; exit 0 ;;
        *) printf 'Unknown arg: %s\n' "$1" >&2; exit 2 ;;
    esac
done

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="gigacode_probe_${TS}.txt"

PROBE_PROMPT='Respond in plain text, no markdown. Line 1: "model=<name>". Line 2: "context_window_tokens=<number or unknown>". Then for each of these tools output exactly one line "<name>: available" or "<name>: not available": Task, WebFetch, run_shell_command, Read, Write, Edit, Grep, Glob. Finally, if any MCP servers are connected, list them one per line as "mcp: <server_name>"; if none, output "mcp: none".'

redact() {
    # Маскируем наиболее вероятные форматы секретов. Это best-effort —
    # пользователь всё равно должен глазами проверить отчёт.
    sed -E \
        -e 's/(sk-[A-Za-z0-9_-]{8,})/[REDACTED-SK]/g' \
        -e 's/(gh[psuor]_[A-Za-z0-9]{16,})/[REDACTED-GH]/g' \
        -e 's/([Bb]earer[[:space:]]+[A-Za-z0-9._-]+)/Bearer [REDACTED]/g' \
        -e 's/([Aa]uthorization:[[:space:]]*[A-Za-z0-9._-]+)/Authorization: [REDACTED]/g' \
        -e 's/([A-Za-z0-9_-]{48,})/[REDACTED-LONG]/g'
}

section() { printf '\n===== %s =====\n' "$1" >> "$OUT"; }

run() {
    printf '\n$ %s\n' "$*" >> "$OUT"
    "$@" 2>&1 | redact >> "$OUT"
}

: > "$OUT"

section "META"
{
    echo "timestamp_utc: $TS"
    echo "uname: $(uname -a)"
    echo "shell: ${SHELL:-unknown}"
    echo "pwd: $PWD"
} >> "$OUT"

section "DISCOVERY"
run which gigacode
run command -v gigacode
# Если это обёртка/алиас — первая строка выдаст shebang или содержимое.
GC_PATH="$(command -v gigacode 2>/dev/null || true)"
if [ -n "$GC_PATH" ] && [ -f "$GC_PATH" ]; then
    printf '\n$ file %s\n' "$GC_PATH" >> "$OUT"
    file "$GC_PATH" 2>&1 | redact >> "$OUT" || true
    printf '\n$ head -5 %s\n' "$GC_PATH" >> "$OUT"
    head -5 "$GC_PATH" 2>&1 | redact >> "$OUT" || true
fi

section "VERSION"
run gigacode --version
run gigacode -v

section "HELP (top-level)"
run gigacode --help
run gigacode -h

section "HELP (subcommands)"
# Only `mcp` and `extensions` are real subcommands in 0.10.0; others echo
# the top-level help (600+ lines of noise). See docs/gigacode-cli-notes.md.
for sub in mcp extensions; do
    printf '\n--- gigacode %s --help ---\n' "$sub" >> "$OUT"
    run gigacode "$sub" --help
done

section "LIVE LISTS (mcp list, extensions list)"
run gigacode mcp list
run gigacode -l

section "ENV (GIGACODE* / QWEN_* / GEMINI_* markers)"
# OPS-018: GigaCode sets GIGACODE=1 and GIGACODE_NO_RELAUNCH=true at runtime.
env | grep -E '^(GIGACODE|QWEN_|GEMINI_)' | redact >> "$OUT" || echo "(none)" >> "$OUT"

section "CONFIG DIRS (listing only, no file contents)"
for d in "$HOME/.gigacode" "$HOME/.config/gigacode" "$HOME/.qwen" "$HOME/.config/qwen"; do
    if [ -d "$d" ]; then
        printf '\n# %s\n' "$d" >> "$OUT"
        ls -la "$d" 2>&1 | awk '{print $1, $5, $NF}' >> "$OUT" || true
        for sub in extensions commands; do
            if [ -d "$d/$sub" ]; then
                printf '\n# %s/%s/\n' "$d" "$sub" >> "$OUT"
                ls -la "$d/$sub" 2>&1 | awk '{print $1, $5, $NF}' >> "$OUT" || true
            fi
        done
        # enablement.json / settings.json — имена видно, содержимое не читаем
        for f in enablement.json settings.json config.json config.yaml config.yml; do
            if [ -f "$d/$f" ]; then
                printf '# present: %s/%s (%d bytes)\n' \
                    "$d" "$f" "$(wc -c < "$d/$f" 2>/dev/null || echo 0)" >> "$OUT"
            fi
        done
    else
        printf '\n# %s : absent\n' "$d" >> "$OUT"
    fi
done

if [ "$LIVE" -eq 1 ]; then
    section "CAPABILITY PROBE (with --allowed-tools=run_shell_command -p)"
    run gigacode --allowed-tools=run_shell_command -p "$PROBE_PROMPT"

    section "CAPABILITY PROBE (with -p only)"
    run gigacode -p "$PROBE_PROMPT"
else
    section "CAPABILITY PROBE"
    echo "(skipped: --no-live)" >> "$OUT"
fi

section "DONE"
printf 'Report written to: %s\n' "$OUT"
printf 'Review it manually for residual secrets before sharing.\n'

echo ""
echo "Report: $OUT"
echo "Next: glance at the file, redact anything the sed-pass missed, then copy it back."
