#!/usr/bin/env python3
r"""polisade_diff_smells.py — advisory-сканер диффа: антипаттерны тестов (#31)
и стендозависимые литералы (#161).

Детерминированный помощник ревьюера и self-review. Он НЕ выносит вердикт и
НИЧЕГО не блокирует: печатает список подозрительных строк диффа, а решение
(«настоящая проблема» / «так и задумано») принимает ревьюер или PM.

ЧЕСТНАЯ ГРАНИЦА (её же печатает `--rules`):

  * **Exit-код всегда 0** — и когда находок нет, и когда их сотня, и когда
    дифф вообще не удалось получить. 0 здесь значит «сканер отработал», а не
    «проблем нет». Различать эти случаи нужно по полю `status`: вход, в
    котором нет ни одного структурного заголовка (`diff --git` либо `+++`) —
    это `status: error`, а НЕ «находок нет». Дифф без текстовых ханков
    (переименование, бинарный файл, смена прав) заголовки имеет и ошибкой не
    считается: там просто нечего сканировать. Флага `--strict` нет
    намеренно: правила ниже — эвристики по одному тексту строки, на таком
    основании нельзя красить сборку.
  * **Сканер видит только дифф.** Утверждение, спрятанное в хелпере за
    пределами диффа; фиксация времени, импортированная в шапке файла;
    значение, вынесенное в конфиг соседним коммитом, — всё это ему не видно.
  * **Каждое правило — эвристика по подстроке/регулярке, а не анализ кода.**
    Ни парсинга языка, ни типов, ни разрешения имён здесь нет. Слепые пятна
    каждого правила перечислены поимённо в таблице ниже — читай их прежде,
    чем считать молчание сканера доказательством.

СЕМЕЙСТВА И РАЗДЕЛЕНИЕ ФАЙЛОВ

Файл попадает РОВНО в одно семейство. Тестоподобный (имя `test_*.py`,
`*_test.*`, `*_tests.*`, `*.test.ts|js`, `*.spec.*`, `*Test.java`,
`*Tests.java`, `*IT.java`, либо путь через сегмент `tests`, `__tests__` или
`src/test`) проверяется только правилами `test-smells`. Остальные исходники —
только правилами `stand-values`. Файлы документации (`.md`, `.rst`, `.txt`,
`.adoc`, `.html`) и бинарные не проверяются ничем.

Конфиги профилей — носители значений, там стендозависимым литералам и место,
поэтому для `stand-values` пропускаются: `.env*`, `env.example`,
`application*.yml|yaml|properties`, любой `*.properties`.

ТАБЛИЦА ПРАВИЛ

  test-smells (только тестоподобные файлы)

  TS-01  sleep-зависимость
         Эвристика: код строки (строковые литералы вычищены) содержит
         `time.sleep`, `Thread.sleep`, `asyncio.sleep`, `setTimeout`,
         `Task.Delay` или вызов `sleep(`.
         Слепые пятна: ожидание, спрятанное в хелпере (`waitFor(...)`), не
         видно; легитимный `setTimeout` внутри мока приложения даёт ложное
         срабатывание.

  TS-02  тест без утверждений
         Эвристика: объявление теста (`def test_*`, `it(`/`test(`, `@Test`,
         `func Test*`) добавлено диффом, тело теста ВИДНО ЦЕЛИКОМ (сканер
         дошёл до его конца по отступу или по балансу скобок), и в теле нет
         ни одного из токенов `assert*`, `expect(`, `assertThat`, `should`,
         `verify(`.
         Новый файл (старая сторона `/dev/null`, и все ханки дочитаны до
         объявленной в `@@` длины) виден целиком, поэтому последний тест в нём
         тоже считается закрытым. Оборванный поток объявленную длину не
         добирает — и такой файл целиком видимым НЕ считается.
         Слепые пятна: собственный хелпер-матчер (`checkUserIsValid(u)`)
         утверждением не считается — ложное срабатывание; тест, чьё тело
         обрезано границей ханка в СУЩЕСТВУЮЩЕМ файле, НЕ проверяется вовсе —
         молчание тут ничего не значит.

  TS-03  только snapshot-утверждение
         Эвристика: в видимом целиком теле теста есть утверждение, но ВСЕ
         утверждения — снапшотные (`toMatchSnapshot`, `toMatchInlineSnapshot`,
         `assertSnapshot`, `verifyApproved`). Считается по выражениям (строка
         режется по `;`), поэтому `expect(v).toMatchSnapshot();
         expect(v.title).toBe("ok")` снапшотом целиком не считается.
         Слепые пятна: снапшот бывает и осознанным выбором (golden-файл
         генератора); правило маркирует форму, а не ошибку. Утверждение,
         разложенное по нескольким физическим строкам, разбирается по первой
         из них, поэтому многострочная снапшот-цепочка не распознаётся.

  TS-04  порядковая зависимость
         Эвристика: код строки (строковые литералы вычищены) содержит
         `@Order`, `@TestMethodOrder`, `@FixMethodOrder`, `dependsOn…`
         в позиции параметра (`dependsOnMethods = …`, `dependsOn(…)` — имя
         переменной `dependsOnCache` не в счёт), `@pytest.mark.order` или
         `.serial`.
         Слепые пятна: порядок, наведённый общим мутируемым состоянием
         (статическое поле, общая БД без отката), никакой строкой не помечен
         и не детектируется.

  TS-05  недетерминизм без фиксации
         Эвристика: строка содержит `random.`, `Math.random`, `datetime.now`,
         `Date.now`, `new Date()`, `LocalDate*.now`, `System.currentTimeMillis`
         или `uuid4`, и рядом нет маркера фиксации (`freezegun`, `freeze_time`,
         `useFakeTimers`, `FakeTimers`, `time_machine`, `Clock.fixed`,
         `MockedStatic`, `seed(`). «Рядом» = видимые строки диффа ЭТОГО
         файла; рабочая копия не читается, чтобы результат зависел только от
         диффа, а не от того, что сейчас лежит на диске.
         Слепые пятна: фиксация через фикстуру/conftest, через
         DI-конфигурацию или импортом за пределами диффа не видна — ложное
         срабатывание; маркер фиксации ЛЮБОГО теста в файле глушит правило
         для всех тестов этого файла; недетерминизм из внешнего источника
         (сеть, файловая система, порядок словаря) не детектируется вовсе.

  stand-values (только НЕ тестоподобные исходники, кроме конфигов профилей)

  SV-01  DSN/JDBC-URL с хостом
         Эвристика: схема из закрытого списка (jdbc, postgres, postgresql,
         mysql, mariadb, mongodb, oracle, sqlserver, redis, amqp, kafka,
         ldap, ibm-db2) + разделитель + непустой хост; либо форма
         `oracle:<драйвер>:@хост`. Хосты `localhost`, `127.0.0.1`, `0.0.0.0`
         исключены.
         Слепые пятна: URL, собранный конкатенацией из частей, не совпадёт с
         регуляркой; строка подключения в тестовом контейнере (Testcontainers
         в НЕ тестовом файле) даст ложное срабатывание.

  SV-02  привязка к схеме БД
         Эвристика: строка содержит присваивание `schema`/`currentSchema` или
         упоминание пути поиска схемы (`search_path`), либо
         `@Table(schema=…)` / `setSchema(…)`.
         Слепые пятна: имя схемы, приклеенное к имени таблицы
         (`"ift_core.users"`), правилом не ловится; конструкция может быть и
         чтением значения из конфига — форма та же.

  SV-03  значение из конфигурации проекта, вшитое в код
         Эвристика: значение ключа вида `*_SCHEMA`, `*_HOST`, `*_NAMESPACE`,
         `*_ENV` (и их camelCase-форм) прочитано из `.env`, `.env.example`,
         `env.example`, `.state/knowledge.json`, `knowledge.json` в
         `--project-root` и встречено в коде как отдельное слово внутри
         строкового литерала.
         Слепые пятна: правило работает ТОЛЬКО если такие файлы есть и в них
         лежат реальные (не `changeme`) значения; значения короче трёх
         символов и очевидные заглушки отбрасываются; совпадение может быть
         случайным омонимом.

  SV-04  IP-адрес в литерале
         Эвристика: четыре октета 0–255 внутри строкового литерала, кроме
         `127.0.0.1`, `0.0.0.0`, `255.255.255.255`.
         Слепые пятна: версия вида «1.2.3.4» неотличима от адреса; адрес,
         собранный из частей или заданный в IPv6, не ловится.

  SV-05  hostname с доменом в литерале
         Эвристика: внутри строкового литерала ищутся доменные имена — в том
         числе в середине URL и перед портом. Принимается имя, у которого
         последняя метка ОБЯЗАТЕЛЬНО из закрытого списка доменов верхнего
         уровня, ни одна метка не начинается с заглавной (это был бы
         Java-пакет) и последняя метка не выглядит расширением файла.
         Слепые пятна: хост во внутренней зоне, которой нет в списке
         (`db.mycorp`), не находится вовсе; короткий хост без домена
         (`db-ift`) — тоже; публичный домен в легитимном контракте
         (`api.github.com`) даёт ложное срабатывание.

  SV-06  порт в строковом литерале
         Эвристика: внутри строкового литерала — двоеточие и 2–5 цифр,
         которым не предшествует цифра (чтобы не ловить время `12:30:00`).
         Литералы с `localhost`/`127.0.0.1`/`0.0.0.0` исключены.
         Литералы с пробелами пропускаются: «HTTP status:404» — фраза, а
         не адрес.
         Слепые пятна: порт, заданный числом без строки (`PORT = 5432`), не
         ловится; сплошной `key:value` без пробелов («HTTP:404») даст ложное
         срабатывание.

  SV-07  абсолютный локальный путь
         Эвристика: домашний каталог macOS/Linux С ИМЕНЕМ ПОЛЬЗОВАТЕЛЯ и хотя
         бы одним НЕПУСТЫМ сегментом после него (HTTP-маршрут с одним
         сегментом после домашнего каталога под правило не подходит — ни с
         завершающим слэшем, ни без него), либо `<буква>:\` (Windows).
         Слепые пятна: абсолютный путь, начинающийся с `/opt`, `/var`, `/srv`,
         не ловится — он бывает и легитимным контрактом развёртывания.

ИСКЛЮЧЕНИЯ, ОБЩИЕ ДЛЯ ВСЕХ ПРАВИЛ

  * Совпадение внутри строчного комментария (`#`, `//` вне кавычек) не
    считается находкой: там живут примеры. Блочный комментарий `/* … */`
    отслеживается для C-подобных языков как многострочное состояние. `//`
    сразу после двоеточия комментарием НЕ считается — это разделитель схемы
    URL, и иначе незакавыченная строка подключения (Java text block, значение
    в YAML) обрезалась бы ровно там, где начинается хост.
  * Строка внутри тройных кавычек не считается находкой ТОЛЬКО в
    Python-подобных файлах: в Java та же последовательность — text block с
    настоящим значением внутри, и глушить там правила нельзя. Состояние
    считается по кодовой части видимых строк диффа (иначе тройная кавычка,
    попавшая в обычный комментарий, переключала бы его) и сбрасывается на разрыве нумерации —
    docstring, открытый ВНЕ диффа, сканер не увидит.
  * Правила TS-01, TS-04, TS-05 смотрят на код строки с ВЫЧИЩЕННЫМИ
    строковыми литералами: `assert msg == "retry via time.sleep(1)"` —
    проверка сообщения, а не ожидание. Правила семейства stand-values
    литералы НЕ вычищают: значение стенда живёт именно в литерале, и
    вычистить его значило бы выключить правило. Цена честно названа в слепых
    пятнах SV-01/SV-02: строка-сообщение с той же формой даёт ложное
    срабатывание.
  * Правила записаны так, чтобы сам сканер не срабатывал на собственном
    перечне: в каждой регулярке между значащими символами стоит класс или
    квантификатор, поэтому исходник правила не совпадает с правилом.

ИСПОЛЬЗОВАНИЕ

    polisade_diff_smells.py --base origin/main
    polisade_diff_smells.py --diff-file /tmp/pr.diff --json
    git diff main...HEAD | polisade_diff_smells.py
    polisade_diff_smells.py --rules
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys

FAMILY_TEST = "test-smells"
FAMILY_STAND = "stand-values"
FAMILIES = (FAMILY_TEST, FAMILY_STAND)

# --------------------------------------------------------------------------
# Классификация файлов
# --------------------------------------------------------------------------

_DOC_EXT = {".md", ".rst", ".txt", ".adoc", ".html", ".htm", ".tex"}
_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".gz",
    ".jar", ".class", ".lock", ".woff", ".woff2", ".ttf", ".eot", ".bin",
}

# Имя файла, по которому файл считается тестоподобным. Стем (имя без
# расширения) проверяется целиком, чтобы `regression_tests.sh` и
# `UserRepositoryTest.java` попали в тесты так же, как `test_user.py`.
_TEST_STEM_RE = re.compile(
    r"(?:^test[_.-])|(?:[_.-]tests?$)|(?:Tests?$)|(?:[a-z]IT$)|(?:^Test[A-Z])"
)
_TEST_INFIX_RE = re.compile(r"\.(?:test|spec)\.", re.IGNORECASE)
_TEST_DIR_PARTS = {"tests", "__tests__", "testing"}


def is_test_path(path):
    """Тестоподобный ли путь. Имя ИЛИ каталог — этого достаточно."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    for i, part in enumerate(parts[:-1]):
        if part in _TEST_DIR_PARTS:
            return True
        # Maven/Gradle: src/test/java/…, а также корневой каталог test/.
        if part == "test" and (i == 0 or parts[i - 1] == "src"):
            return True
    if _TEST_INFIX_RE.search(name):
        return True
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return bool(_TEST_STEM_RE.search(stem))


_APPLICATION_CFG_RE = re.compile(
    r"^application(?:[-_][A-Za-z0-9._-]+)?\.(?:ya?ml|properties)$", re.IGNORECASE
)


def is_config_carrier(path):
    """Конфиг профиля: стендозависимым значениям там и место (issue #161)."""
    name = posixpath.basename(path)
    low = name.lower()
    if low.startswith(".env"):
        return True
    if low == "env.example" or low.endswith(".env"):
        return True
    if low.endswith(".properties"):
        return True
    return bool(_APPLICATION_CFG_RE.match(name))


def _ext(path):
    name = posixpath.basename(path)
    return ("." + name.rsplit(".", 1)[1].lower()) if "." in name[1:] else ""


def family_of(path):
    """Семейство правил для файла, либо None — файл не сканируется."""
    ext = _ext(path)
    if ext in _DOC_EXT or ext in _BINARY_EXT:
        return None
    if is_test_path(path):
        return FAMILY_TEST
    if is_config_carrier(path):
        return None
    return FAMILY_STAND


# --------------------------------------------------------------------------
# Разбор unified diff
# --------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@+ .*?\+(\d+)(?:,(\d+))? @@")


def _unquote_git_path(raw):
    """`"b/\\320\\272"` → `b/к`. git цитирует не-ASCII пути по умолчанию."""
    if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt.isdigit():
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            out.extend({"n": b"\n", "t": b"\t", '"': b'"', "\\": b"\\"}.get(nxt, nxt.encode()))
            i += 2
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _header_path(raw):
    """Путь из строки `--- a/x` / `+++ b/x`, без хвостового timestamp.

    Обычный `diff -u` (не git) пишет `+++ path\\t2026-09-06 12:00:00`, и
    склеенный timestamp делал бы координату находки неоткрываемой.
    """
    target = raw[4:].split("\t", 1)[0].strip()
    if target.startswith('"'):
        target = _unquote_git_path(target)
    if target in ("/dev/null", ""):
        return None
    return target[2:] if target[:2] in ("b/", "a/") else target


def parse_unified_diff(text):
    """→ (files, complete, headers), где files = {path: [(lineno, kind, text)]}.

    * `kind ∈ {'+', ' '}` — контекстные строки сохраняются: тело теста и маркер
      фиксации времени часто попадают именно в них, а находки всё равно
      печатаются только по добавленным.
    * `complete` — пути, которые видны диффом ЦЕЛИКОМ: старая сторона
      `/dev/null` И все ханки дочитаны до объявленной длины. Для них конец
      видимых строк совпадает с концом файла.
    * `headers` — сколько структурных заголовков (`diff --git` либо `+++`)
      удалось разобрать. Ноль означает «это не дифф», а не «находок нет»;
      дифф без текстовых ханков (переименование, бинарный файл, смена прав)
      структурные заголовки имеет, поэтому ошибкой не считается.

    Структура читается по СЧЁТЧИКУ ДЛИНЫ ХАНКА: пока объявленные `@@ … +c,d @@`
    строки не дочитаны, любая строка — содержимое. Без счётчика содержимое
    вида `+++ …` или `--- …` (дифф, положенный в фикстуру или в тело письма)
    переключало разбор на несуществующий файл.
    """
    files = {}
    complete = set()
    truncated = set()
    headers = 0
    cur = None
    lineno = 0
    remaining = 0
    old_is_devnull = False
    for raw in text.splitlines():
        if remaining <= 0:
            if raw.startswith("diff --git"):
                if cur is not None and remaining > 0:
                    truncated.add(cur)
                cur = None
                headers += 1
                old_is_devnull = False
                continue
            if raw.startswith("--- "):
                old_is_devnull = _header_path(raw) is None
                continue
            if raw.startswith("+++ "):
                headers += 1
                path = _header_path(raw)
                if path is None:        # удаление файла — сканировать нечего
                    cur = None
                    continue
                cur = path
                files.setdefault(cur, [])
                if old_is_devnull:
                    complete.add(cur)
                continue
            m = _HUNK_RE.match(raw)
            if m:
                lineno = int(m.group(1))
                remaining = int(m.group(2)) if m.group(2) is not None else 1
                continue
            if cur is None:
                continue
        if cur is None:
            continue
        if raw.startswith("+"):
            files[cur].append((lineno, "+", raw[1:]))
            lineno += 1
            remaining -= 1
        elif raw.startswith("\\"):
            continue
        elif raw.startswith("-"):
            continue
        elif raw.startswith(" ") or raw == "":
            files[cur].append((lineno, " ", raw[1:] if raw else ""))
            lineno += 1
            remaining -= 1
        else:
            # Не строка ханка при незакрытом счётчике — вход оборван.
            truncated.add(cur)
            remaining = 0
    if cur is not None and remaining > 0:
        truncated.add(cur)
    return ({p: v for p, v in files.items() if v},
            complete - truncated, headers)


# --------------------------------------------------------------------------
# Кавычки, комментарии, docstring
# --------------------------------------------------------------------------

_COMMENT_TOKENS = {
    ".py": ("#",), ".sh": ("#",), ".bash": ("#",), ".zsh": ("#",),
    ".rb": ("#",), ".yml": ("#",), ".yaml": ("#",), ".pl": ("#",),
    ".r": ("#",), ".tf": ("#",), ".toml": ("#",), ".ini": (";", "#"),
    ".sql": ("--",),
}
_DEFAULT_COMMENT_TOKENS = ("//", "#")


# Языки, где тройная кавычка открывает ДОКУМЕНТАЦИЮ. В Java/Kotlin та же
# последовательность — text block с настоящим значением внутри (SQL, URL), и
# принимать его за docstring значило бы молча выключить там все правила.
_DOCSTRING_EXT = {".py", ".pyi", ".pyw", ""}
# Языки с блочным комментарием. Он многострочный, поэтому требует состояния —
# иначе закомментированный `Thread.sleep(1)` читается как код.
_BLOCK_COMMENT_EXT = {
    ".java", ".kt", ".kts", ".scala", ".js", ".jsx", ".ts", ".tsx", ".mjs",
    ".cjs", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".php", ".css", ".scss",
    ".swift", ".rs",
}
# Схема URL перед двойным слэшем: `jdbc:postgresql://host` — разделитель, а не
# начало комментария. Проверяется именно СХЕМА, а не «предыдущий символ —
# двоеточие»: иначе `case 1: // комментарий` в Java переставал бы быть
# комментарием (находка круга 2).
_SCHEME_TAIL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:$")
_TRIPLE_DELIMS = ('"' * 3, "'" * 3)


def clean_line(line, tokens, state, triple_ok, block_ok):
    """→ (код строки без комментариев, новое состояние).

    Комментарии — строчные, блочные и многострочные docstring — заменяются
    пробелами ПОСИМВОЛЬНО, а не выключают строку целиком: круг 2 показал, что
    флаг «вся строка подавлена» одновременно пропускал закомментированный
    вызов рядом с настоящим и глушил настоящий вызов после закрывающего
    маркера.

    Строковые литералы СОХРАНЯЮТСЯ: правила SV-03..SV-06 смотрят именно
    внутрь них. Тройная кавычка, открытая и закрытая на одной строке, — это
    ЗНАЧЕНИЕ, а не docstring, и тоже сохраняется.

    `state` = (внутри блочного комментария, открытый тройной разделитель);
    передаётся от строки к строке.
    """
    in_block, doc_delim = state
    out = []
    i = 0
    n = len(line)
    while i < n:
        if in_block:
            j = line.find("*/", i)
            if j < 0:
                out.append(" " * (n - i))
                i = n
            else:
                out.append(" " * (j + 2 - i))
                i = j + 2
                in_block = False
            continue
        if doc_delim:
            j = line.find(doc_delim, i)
            if j < 0:
                out.append(" " * (n - i))
                i = n
            else:
                out.append(" " * (j + 3 - i))
                i = j + 3
                doc_delim = None
            continue
        ch = line[i]
        if triple_ok and any(line.startswith(d, i) for d in _TRIPLE_DELIMS):
            delim = line[i:i + 3]
            j = line.find(delim, i + 3)
            if j < 0:
                doc_delim = delim
                out.append(" " * (n - i))
                i = n
            else:
                out.append(line[i:j + 3])
                i = j + 3
            continue
        if block_ok and line.startswith("/*", i):
            j = line.find("*/", i + 2)
            if j < 0:
                in_block = True
                out.append(" " * (n - i))
                i = n
            else:
                out.append(" " * (j + 2 - i))
                i = j + 2
            continue
        if ch in "\"'`":
            j = i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == ch:
                    j += 1
                    break
                j += 1
            out.append(line[i:j])
            i = j
            continue
        hit = None
        for tok in tokens:
            if line.startswith(tok, i):
                if tok == "//" and _SCHEME_TAIL_RE.search(line[:i]):
                    continue
                hit = tok
                break
        if hit:
            out.append(" " * (n - i))
            i = n
            continue
        out.append(ch)
        i += 1
    return "".join(out), (in_block, doc_delim)


def code_views(visible, ext, tokens):
    """Код каждой видимой строки без комментариев и docstring.

    Состояние СБРАСЫВАЕТСЯ на разрыве нумерации: незакрытый комментарий в
    одном ханке не должен «заражать» соседний.
    """
    triple_ok = ext in _DOCSTRING_EXT
    block_ok = ext in _BLOCK_COMMENT_EXT
    views = []
    state = (False, None)
    prev = None
    for lineno, _kind, text in visible:
        if prev is not None and lineno != prev + 1:
            state = (False, None)
        prev = lineno
        code, state = clean_line(text, tokens, state, triple_ok, block_ok)
        views.append(code)
    return views


_LITERAL_RE = re.compile(
    r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`"
)


def literals(line):
    """[(start, end, inner)] — строковые литералы строки."""
    out = []
    for m in _LITERAL_RE.finditer(line):
        out.append((m.start(), m.end(), m.group(0)[1:-1]))
    return out


# --------------------------------------------------------------------------
# Правила
# --------------------------------------------------------------------------
#
# Каждая регулярка записана так, чтобы её собственный исходник ей не
# соответствовал: между значащими символами стоит класс, квантификатор или
# группа. Иначе сканер стабильно находил бы «проблемы» в самом себе.

_RULE_TITLES = {
    "TS-01": "sleep-зависимость",
    "TS-02": "тест без утверждений",
    "TS-03": "только snapshot-утверждение",
    "TS-04": "порядковая зависимость",
    "TS-05": "недетерминизм без фиксации",
    "SV-01": "DSN/JDBC-URL с хостом",
    "SV-02": "привязка к схеме БД",
    "SV-03": "значение из конфигурации проекта в коде",
    "SV-04": "IP-адрес в литерале",
    "SV-05": "hostname с доменом в литерале",
    "SV-06": "порт в строковом литерале",
    "SV-07": "абсолютный локальный путь",
}

_TS01_RE = re.compile(
    r"(?:time|asyncio|Thread|TimeUnit\.\w+)\s*\.\s*[sS]leep\s*\("
    r"|\bset[Tt]imeout\s*\("
    r"|\bTask\s*\.\s*Delay\s*\("
    r"|(?:^|[^\w.])sleep\s*\("
)
_TS04_RE = re.compile(
    r"@(?:Order|TestMethodOrder|FixMethodOrder)\b"
    r"|\bdepends[Oo]n(?:Methods|Groups)?\s*[=(:]"
    r"|@pytest\.mark\.order\b"
    r"|\.serial\b"
)
_TS05_RE = re.compile(
    r"\brandom\s*\.\s*\w"
    r"|\bMath\s*\.\s*random\s*\("
    r"|\bdatetime\s*\.\s*(?:datetime\s*\.\s*)?now\s*\("
    r"|\bDate\s*\.\s*now\s*\("
    r"|\bnew\s+Date\s*\(\s*\)"
    r"|\bLocalDate(?:Time)?\s*\.\s*now\s*\("
    r"|\bInstant\s*\.\s*now\s*\("
    r"|\bSystem\s*\.\s*currentTimeMillis\s*\("
    r"|\buuid[1-5]\s*\("
)
_TS05_FREEZE_RE = re.compile(
    r"freeze[_g]?un|freeze[_-]?time|use[Ff]ake[Tt]imers|[Ff]ake[Tt]imers"
    r"|time[_-]machine|Clock\s*\.\s*fixed|Mocked[Ss]tatic|[Ss]eed\s*\("
    r"|patch\([^)]*(?:now|time)"
)

_ASSERT_RE = re.compile(
    r"\bassert\w*\b|\bexpect\s*\(|\bassert[_A-Z]\w*\s*\(|\bassertThat\b"
    r"|\bshould\b|\bverify\s*\(|\bverifyApproved\s*\(|\bassert\s*\("
)
_SNAPSHOT_RE = re.compile(
    r"to[Mm]atch(?:Inline)?[Ss]napshot\s*\(|assert[_]?[Ss]napshot\s*\("
    r"|verify[Aa]pproved\s*\("
)

_TEST_DECL_RES = (
    re.compile(r"^\s*(?:async\s+)?def\s+test\w*\s*\("),
    re.compile(r"^\s*(?:await\s+)?(?:it|test)(?:\.\w+)*\s*\("),
    re.compile(r"^\s*@(?:Test|ParameterizedTest|RepeatedTest)\b"),
    re.compile(r"^\s*func\s+Test\w*\s*\("),
)

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "255.255.255.255")

_DSN_SCHEMES = (
    "jdbc", "postgres", "postgresql", "mysql", "mariadb", "mongodb",
    "mongodb+srv", "oracle", "sqlserver", "redis", "rediss", "amqp",
    "amqps", "kafka", "ldap", "ldaps", "db2",
)
_SV01_RE = re.compile(
    r"(?i)\b(?:" + "|".join(re.escape(s) for s in _DSN_SCHEMES) + r")"
    r"(?::[a-z0-9_]+)?:/{2}(?P<host>[A-Za-z0-9_.\-]+)"
)
_SV01_ORACLE_RE = re.compile(r"(?i)\boracle:[a-z]+:[@](?P<host>[A-Za-z0-9_.\-]+)")
_SV02_RE = re.compile(
    r"(?i)(?:current)?schema\s*[=:]\s*[\"'A-Za-z0-9_]"
    r"|search[_-]?path"
    r"|set[Ss]chema\s*\("
    r"|@Table\s*\([^)]*schema"
)
_SV04_RE = re.compile(r"\b(?P<a>\d{1,3})\.(?P<b>\d{1,3})\.(?P<c>\d{1,3})\.(?P<d>\d{1,3})\b")
_SV06_RE = re.compile(r"(?<![0-9])[:](?P<port>\d{2,5})(?![0-9])")
_SV07_RE = re.compile(
    r"/(?:Users|home)/[A-Za-z0-9._-]+/[A-Za-z0-9._-]"
    r"|\b[A-Za-z]:[\\]{1,2}[A-Za-z]"
)

# Последняя метка доменного имени: закрытый список — иначе любое `foo.bar`
# из кода станет «хостом».
_TLDS = {
    "com", "net", "org", "ru", "io", "dev", "co", "uk", "de", "fr", "eu",
    "edu", "gov", "mil", "int", "info", "biz", "local", "localdomain",
    "corp", "internal", "intranet", "lan", "cloud", "app", "ai", "su",
}
# Последняя метка, которая почти наверняка расширение файла, а не домен.
_FILE_EXTS = {
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "json", "yml", "yaml",
    "md", "rst", "txt", "sh", "bash", "xml", "html", "htm", "css", "scss",
    "java", "kt", "kts", "go", "rb", "php", "sql", "env", "example",
    "lock", "toml", "ini", "cfg", "properties", "tf", "gradle", "log",
    "csv", "png", "svg", "jpg", "gif", "pdf", "zip", "jar", "class",
    "tpl", "j2", "conf", "sample", "template", "bak", "orig", "diff",
    "patch", "gz", "tar", "min", "map", "d", "h", "c", "cpp", "hpp", "rs",
}
# Кандидат в доменные имена ВНУТРИ литерала: хост живёт и в середине URL
# (`https://db.corp.example.com/api`), и перед портом (`db.corp.example.com:5432`),
# а не только как литерал целиком.
_HOSTNAME_CANDIDATE_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)


def _looks_like_hostname(value):
    """Строгий приём: последняя метка ОБЯЗАНА быть доменом верхнего уровня.

    Прежняя редакция принимала «три метки и больше» как альтернативу, и
    поэтому объявляла хостом любой Java-пакет в нижнем регистре
    (`com.example.orders`) — оба ревьюера нашли это независимо. Цена
    строгости названа в слепых пятнах: хост во внутренней зоне, которой нет
    в списке, теперь не находится вовсе.
    """
    value = value.strip()
    if not value or len(value) > 253:
        return False
    labels = value.split(".")
    if any(lbl[:1].isupper() for lbl in labels):  # com.example.Foo — пакет
        return False
    last = labels[-1].lower()
    if not last.isalpha() or len(last) < 2 or len(last) > 24:
        return False
    if last in _FILE_EXTS:
        return False
    if value.lower() in _LOCAL_HOSTS:
        return False
    return last in _TLDS


def hostname_candidates(inner):
    """Доменные имена внутри строкового литерала, прошедшие приём."""
    return [m.group(0) for m in _HOSTNAME_CANDIDATE_RE.finditer(inner)
            if _looks_like_hostname(m.group(0))]


# --------------------------------------------------------------------------
# Словарь стендозависимых имён проекта (SV-03)
# --------------------------------------------------------------------------

_VOCAB_KEY_RE = re.compile(r"(?:^|_)(?:SCHEMA|HOST|HOSTNAME|NAMESPACE|ENV)$")
_VOCAB_PLACEHOLDERS = {
    "changeme", "change_me", "todo", "none", "null", "true", "false",
    "example", "sample", "your_value", "value", "xxx", "secret", "password",
}


def _normalize_key(key):
    """`dbSchema` → `DB_SCHEMA`, `DB_SCHEMA` → `DB_SCHEMA`."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    return re.sub(r"[^A-Za-z0-9]+", "_", spaced).upper().strip("_")


def _vocab_accept(value):
    if not isinstance(value, str):
        return False
    value = value.strip()
    if len(value) < 3 or len(value) > 64:
        return False
    if value.lower() in _VOCAB_PLACEHOLDERS or value.lower() in _LOCAL_HOSTS:
        return False
    if value.startswith("<") or value.startswith("${") or value.startswith("$"):
        return False
    if value.isdigit():
        return False
    return True


def _vocab_from_env(text, source):
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lstrip("export ").strip()
        value = value.strip().strip("\"'")
        if _VOCAB_KEY_RE.search(_normalize_key(key)) and _vocab_accept(value):
            out.append({"key": key.strip(), "value": value, "source": source})
    return out


def _vocab_from_json(data, source, out, key=None):
    if isinstance(data, dict):
        for k, v in data.items():
            _vocab_from_json(v, source, out, k)
    elif isinstance(data, list):
        for item in data:
            _vocab_from_json(item, source, out, key)
    elif key is not None and _VOCAB_KEY_RE.search(_normalize_key(key)):
        if _vocab_accept(data):
            out.append({"key": str(key), "value": data.strip(), "source": source})


def project_vocabulary(project_root):
    """Значения стендозависимых ключей из конфигурации проекта."""
    out = []
    if not project_root:
        return out
    candidates = (
        ".env", ".env.example", "env.example",
        os.path.join(".state", "knowledge.json"), "knowledge.json",
    )
    for rel in candidates:
        path = os.path.join(project_root, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if rel.endswith(".json"):
            try:
                _vocab_from_json(json.loads(text), rel, out)
            except (ValueError, TypeError):
                continue
        else:
            out.extend(_vocab_from_env(text, rel))
    seen = set()
    uniq = []
    for item in out:
        if item["value"].lower() in seen:
            continue
        seen.add(item["value"].lower())
        uniq.append(item)
    return uniq


# --------------------------------------------------------------------------
# Тела тестов (TS-02 / TS-03)
# --------------------------------------------------------------------------


def _strip_literals(code):
    """Код без содержимого строковых литералов.

    Нужен правилам, которые описывают КОНСТРУКЦИЮ, а не значение: `assert msg
    == "retry via time.sleep(1)"` — проверка сообщения, а не ожидание.
    Правила SV-03..SV-06, наоборот, смотрят внутрь литералов и этой очистки
    не применяют.
    """
    return _LITERAL_RE.sub('""', code)


def test_blocks(visible, views, python_like, complete=False):
    """[(decl_index, body_slice, closed)] по видимым строкам одного файла.

    `closed` = сканер увидел конец тела (по отступу или по балансу скобок).
    Тест с обрезанным телом не оценивается вовсе — иначе правило «нет
    утверждений» краснело бы на каждом втором ханке.

    `complete` — файл добавлен целиком (старая сторона `/dev/null`), поэтому
    конец видимых строк ЕСТЬ конец файла: последний тест в таком файле закрыт
    по определению. Без этого самый частый случай — новый тестовый файл, где
    последний тест без утверждений — молча пропускался (обе рецензии).
    """
    blocks = []
    n = len(visible)
    decls = [
        i for i in range(n)
        if any(rx.match(visible[i][2]) for rx in _TEST_DECL_RES)
    ]
    for pos, i in enumerate(decls):
        run_end = n
        for lineno_idx in range(i + 1, n):
            if visible[lineno_idx][0] != visible[lineno_idx - 1][0] + 1:
                run_end = lineno_idx
                break
        next_decl = decls[pos + 1] if pos + 1 < len(decls) else run_end
        limit = min(run_end, next_decl if next_decl > i else run_end)
        decl_text = visible[i][2]
        closed = False
        end = limit
        if python_like:
            indent = len(decl_text) - len(decl_text.lstrip())
            for j in range(i + 1, limit):
                txt = visible[j][2]
                if not txt.strip():
                    continue
                if len(txt) - len(txt.lstrip()) <= indent:
                    end = j
                    closed = True
                    break
        else:
            balance = 0
            opened = False
            for j in range(i, limit):
                code = _strip_literals(views[j])
                balance += code.count("{") - code.count("}")
                if balance > 0:
                    opened = True
                elif opened and balance <= 0:
                    end = j + 1
                    closed = True
                    break
        if not closed and limit == next_decl and next_decl < run_end:
            # Следующее объявление теста — тоже конец тела.
            end = next_decl
            closed = True
        if not closed and complete and limit == n:
            # Файл виден целиком, и тело упирается в его конец — это конец
            # теста, а не обрыв ханка.
            end = limit
            closed = True
        blocks.append((i, (i + 1, end), closed))
    return blocks


# --------------------------------------------------------------------------
# Сканирование
# --------------------------------------------------------------------------


def _finding(path, lineno, family, rule, snippet, detail=""):
    return {
        "file": path,
        "line": lineno,
        "family": family,
        "rule": rule,
        "title": _RULE_TITLES[rule],
        "snippet": snippet.strip()[:200],
        "detail": detail,
    }


def scan_test_file(path, visible, complete):
    ext = _ext(path)
    tokens = _COMMENT_TOKENS.get(ext, _DEFAULT_COMMENT_TOKENS)
    views = code_views(visible, ext, tokens)
    findings = []

    # Маркер фиксации ищется в ВИДИМЫХ строках диффа этого файла и только в них.
    # Прежняя редакция читала файл из рабочей копии, и результат сканера зависел
    # от того, что сейчас лежит на диске, а не от диффа (обе рецензии).
    freeze_seen = _TS05_FREEZE_RE.search("\n".join(views))

    for idx, (lineno, kind, text) in enumerate(visible):
        if kind != "+" or not views[idx].strip():
            continue
        code = _strip_literals(views[idx])
        if _TS01_RE.search(code):
            findings.append(_finding(path, lineno, FAMILY_TEST, "TS-01", text))
        if _TS04_RE.search(code):
            findings.append(_finding(path, lineno, FAMILY_TEST, "TS-04", text))
        if _TS05_RE.search(code) and not freeze_seen:
            findings.append(_finding(
                path, lineno, FAMILY_TEST, "TS-05", text,
                "маркер фиксации времени/сида в диффе этого файла не найден",
            ))

    python_like = ext in (".py", ".rb", ".yml", ".yaml") or ext == ""
    for decl, (bstart, bend), closed in test_blocks(visible, views, python_like,
                                                    complete):
        lineno, kind, text = visible[decl]
        if kind != "+" or not views[decl].strip() or not closed:
            continue
        body = views[bstart:bend]
        if not any(b.strip() for b in body):
            continue
        asserts = [b for b in body if _ASSERT_RE.search(b)]
        if not asserts:
            findings.append(_finding(
                path, lineno, FAMILY_TEST, "TS-02", text,
                "в видимом теле теста нет assert/expect/assertThat/should/verify",
            ))
        # «Только снапшот» считается по ВЫРАЖЕНИЯМ, а не по строкам: строка
        # `expect(v).toMatchSnapshot(); expect(v.title).toBe("ok")` содержит и
        # то и другое, и снапшотом целиком не является (обе рецензии).
        # Резать по `;` достаточно: цепочка `expect(x).toMatchSnapshot()` —
        # одно выражение, и её `expect(` не должен считаться отдельным
        # не-снапшотным утверждением.
        elif not any(_ASSERT_RE.search(stmt) and not _SNAPSHOT_RE.search(stmt)
                     for b in asserts for stmt in b.split(";")):
            findings.append(_finding(
                path, lineno, FAMILY_TEST, "TS-03", text,
                "единственное утверждение — снапшот",
            ))
    return findings


def scan_stand_file(path, visible, vocabulary):
    ext = _ext(path)
    tokens = _COMMENT_TOKENS.get(ext, _DEFAULT_COMMENT_TOKENS)
    views = code_views(visible, ext, tokens)
    findings = []
    for idx, (lineno, kind, text) in enumerate(visible):
        if kind != "+":
            continue
        code = views[idx]
        if not code.strip():
            continue
        lits = literals(code)

        # Все совпадения схемы, а не первое: локальный DSN в начале строки не
        # должен заслонять удалённый за ним (круг 2). Литералы, внутри которых
        # найден DSN, помечаются — SV-05/SV-06 их пропускают, потому что это
        # те же байты; НЕЗАВИСИМЫЙ второй литерал на той же строке при этом
        # проверяется как обычно.
        dsn_spans = []
        remote = None
        for rx in (_SV01_RE, _SV01_ORACLE_RE):
            for m in rx.finditer(code):
                dsn_spans.append(m.span())
                if remote is None and m.group("host").lower() not in _LOCAL_HOSTS:
                    remote = m.group("host")
        if remote:
            findings.append(_finding(path, lineno, FAMILY_STAND, "SV-01", text,
                                     "хост: " + remote))

        def _in_dsn(s, e):
            return any(not (e <= ds or s >= de) for ds, de in dsn_spans)

        if _SV02_RE.search(code):
            findings.append(_finding(path, lineno, FAMILY_STAND, "SV-02", text))

        for item in vocabulary:
            value = item["value"]
            pattern = r"(?<![\w-])" + re.escape(value) + r"(?![\w-])"
            if any(re.search(pattern, inner, re.IGNORECASE) for _s, _e, inner in lits):
                # В detail печатается КЛЮЧ и источник, но не значение: вывод
                # сканера уезжает в комментарий к PR, и тащить туда содержимое
                # `.env` не нужно — сама строка кода уже показана в snippet.
                findings.append(_finding(
                    path, lineno, FAMILY_STAND, "SV-03", text,
                    "совпадает со значением %s (%s)" % (item["key"], item["source"]),
                ))
                break

        # Все совпадения, а не первое: невалидный `999.1.1.1` в начале строки
        # больше не заслоняет настоящий адрес за ним (обе рецензии).
        for _s, _e, inner in lits:
            hit = None
            for m in _SV04_RE.finditer(inner):
                if all(int(m.group(g)) <= 255 for g in ("a", "b", "c", "d")) \
                        and m.group(0) not in _LOCAL_HOSTS:
                    hit = m.group(0)
                    break
            if hit:
                findings.append(_finding(path, lineno, FAMILY_STAND, "SV-04",
                                         text, "адрес: " + hit))
                break

        # Хост и порт ВНУТРИ уже найденного DSN — те же байты, что в SV-01;
        # три находки на одну строку — шум, а не сигнал.
        for _s, _e, inner in lits:
            hosts = [] if _in_dsn(_s, _e) else hostname_candidates(inner)
            if hosts:
                findings.append(_finding(path, lineno, FAMILY_STAND, "SV-05",
                                         text, "имя: " + hosts[0]))
                break

        for _s, _e, inner in lits:
            low = inner.lower()
            if any(h in low for h in _LOCAL_HOSTS):
                continue
            # Литерал с пробелами — это фраза, а не адрес: «HTTP status:404»
            # больше не считается портом (обе рецензии).
            if _in_dsn(_s, _e) or not inner.strip() \
                    or any(ch.isspace() for ch in inner):
                continue
            m = _SV06_RE.search(inner)
            if m:
                findings.append(_finding(path, lineno, FAMILY_STAND, "SV-06",
                                         text, "порт: " + m.group("port")))
                break

        if _SV07_RE.search(code):
            findings.append(_finding(path, lineno, FAMILY_STAND, "SV-07", text))
    return findings


def scan_diff(diff_text, families, project_root):
    """→ (findings, scanned, vocabulary, headers).

    `headers == 0` означает «на входе не дифф» (пустая строка, оборванный вывод,
    сорвавшийся `pr-diff`). Вызывающий обязан превратить это в ошибку, а не в
    «находок нет»: обе рецензии показали, что пустой `PR_DIFF` иначе даёт
    ложно-зелёный отчёт.
    """
    files, complete, headers = parse_unified_diff(diff_text)
    vocabulary = project_vocabulary(project_root) if FAMILY_STAND in families else []
    findings = []
    scanned = {FAMILY_TEST: 0, FAMILY_STAND: 0}
    for path in sorted(files):
        family = family_of(path)
        if family is None or family not in families:
            continue
        scanned[family] += 1
        visible = files[path]
        if family == FAMILY_TEST:
            findings.extend(scan_test_file(path, visible, path in complete))
        else:
            findings.extend(scan_stand_file(path, visible, vocabulary))
    findings.sort(key=lambda f: (f["file"], f["line"], f["rule"]))
    return findings, scanned, vocabulary, headers


# --------------------------------------------------------------------------
# Ввод
# --------------------------------------------------------------------------


def diff_from_base(base, project_root):
    """→ (text, error). Ошибка возвращается, а не бросается: exit всегда 0."""
    cmd = ["git", "-C", project_root or ".", "diff", "%s...HEAD" % base]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, ValueError) as exc:
        return "", "git не запустился: %s" % exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return "", "git diff %s...HEAD вернул %d: %s" % (
            base, proc.returncode, detail[0] if detail else "без вывода")
    return proc.stdout, ""


def _read_diff_file(path):
    if path == "-":
        return sys.stdin.read(), ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(), ""
    except OSError as exc:
        return "", "не читается файл диффа %s: %s" % (path, exc)


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------

_ADVISORY = (
    "advisory: сканер не выносит вердикт и ничего не блокирует — "
    "решение принимает ревьюер"
)


def render_text(result):
    out = []
    if result["status"] == "error":
        out.append("⛔ сканер не получил дифф: %s" % result["error"])
        out.append("   Это НЕ значит «находок нет» — доложи причину как есть.")
        return "\n".join(out)
    for f in result["findings"]:
        line = "%s:%d: %s: %s %s: %s" % (
            f["file"], f["line"], f["family"], f["rule"], f["title"], f["snippet"])
        if f["detail"]:
            line += "  [%s]" % f["detail"]
        out.append(line)
    counts = result["counts"]
    if not result["findings"]:
        out.append("находок нет (просмотрено файлов: test-smells %d, stand-values %d)"
                   % (result["scanned"][FAMILY_TEST], result["scanned"][FAMILY_STAND]))
    else:
        out.append("— находок: %d (test-smells: %d, stand-values: %d)"
                   % (len(result["findings"]), counts.get(FAMILY_TEST, 0),
                      counts.get(FAMILY_STAND, 0)))
    if FAMILY_STAND in result["families"]:
        if result["vocabulary"]:
            out.append("словарь стендозависимых имён: %d значени(я/й) из %s"
                       % (len(result["vocabulary"]),
                          ", ".join(sorted({v["source"] for v in result["vocabulary"]}))))
        else:
            out.append("словарь стендозависимых имён пуст: "
                       ".env/.env.example/knowledge.json не найдены или без значений "
                       "— правило SV-03 молчало")
    out.append(_ADVISORY)
    return "\n".join(out)


def rules_text():
    return (__doc__ or "").strip()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="polisade_diff_smells.py",
        description=(
            "Advisory-сканер диффа: антипаттерны тестов (test-smells, #31) и "
            "стендозависимые литералы (stand-values, #161). Exit всегда 0 — "
            "это подсказка ревьюеру, а не гейт. Полная таблица правил с "
            "эвристиками и слепыми пятнами: --rules."
        ),
    )
    parser.add_argument("--base", help="ревизия базы: git diff <base>...HEAD")
    parser.add_argument("--diff-file", help="файл с unified diff, `-` — stdin")
    parser.add_argument("--project-root", default=".",
                        help="корень проекта для git и для словаря SV-03")
    parser.add_argument("--family", choices=list(FAMILIES) + ["all"], default="all")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--rules", action="store_true",
                        help="напечатать таблицу правил и выйти")
    args = parser.parse_args(argv)

    if args.rules:
        print(rules_text())
        return 0

    families = tuple(FAMILIES) if args.family == "all" else (args.family,)
    project_root = args.project_root or "."

    error = ""
    diff_text = ""
    if args.diff_file:
        source = "diff-file"
        diff_text, error = _read_diff_file(args.diff_file)
    elif args.base:
        source = "git-diff"
        diff_text, error = diff_from_base(args.base, project_root)
    elif not sys.stdin.isatty():
        source = "stdin"
        diff_text = sys.stdin.read()
    else:
        source = "none"
        error = "не задан ни --base, ни --diff-file, и на stdin ничего нет"

    if error:
        result = {
            "status": "error", "error": error, "source": source,
            "base": args.base, "families": list(families), "findings": [],
            "counts": {}, "scanned": {FAMILY_TEST: 0, FAMILY_STAND: 0},
            "vocabulary": [], "advisory": _ADVISORY,
        }
    else:
        findings, scanned, vocabulary, headers = scan_diff(
            diff_text, families, project_root)
        counts = {fam: sum(1 for f in findings if f["family"] == fam) for fam in families}
        # `vocabulary` уезжает в комментарий к PR вместе с остальным выводом,
        # поэтому наружу отдаются ключи и источники, но не значения.
        vocab_public = [{"key": v["key"], "source": v["source"]} for v in vocabulary]
        result = {
            "status": "ok" if headers else "error",
            "error": None if headers else (
                "во входных данных нет ни одного заголовка файла — это не дифф "
                "(пустой вывод pr-diff, оборванный поток, не тот аргумент)"),
            "source": source,
            "base": args.base, "families": list(families),
            "findings": findings if headers else [],
            "counts": counts if headers else {},
            "scanned": scanned if headers else {FAMILY_TEST: 0, FAMILY_STAND: 0},
            "vocabulary": vocab_public if headers else [],
            "advisory": _ADVISORY,
        }

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    # Exit всегда 0 — см. «ЧЕСТНАЯ ГРАНИЦА» в докстринге модуля.
    return 0


if __name__ == "__main__":
    sys.exit(main())
