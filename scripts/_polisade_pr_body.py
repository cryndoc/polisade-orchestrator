#!/usr/bin/env python3
"""Тело PR собирает ИНСТРУМЕНТ, а не `git log -1` (issue #380).

Рецепты `/polisade:migrate` и `/polisade:sync` делали телом PR последнее
сообщение коммита, поэтому PR миграции схемы был описан ОДНОЙ строкой
заголовка: список применённых миграций, число затронутых путей, вопросы PM,
неразрешённые ссылки и состояние режимов оставались в консоли той сессии.
Ревьюер при этом судит о таком PR по диффу на десятки файлов — миграция
единственная трогает `.state/`, переносит ADR между каталогами и добавляет
шаблоны. Замерено на корп-прогоне: 21 файл в первом коммите, тело в одну
строку, девять вопросов PM не видны вовсе.

Это тот же замеренный рычаг, что и готовая строка `summary` (issue #354),
только на шаг дальше: не «модель берёт число из JSON», а «инструмент кладёт
готовый markdown, модель его не пересказывает».

Модуль общий, потому что контракт ОДИН на два инструмента: форма опции, форма
поля `pr_body` в ответе и текст отказа. Правило, записанное дважды, расходится
молча — и расходится в сторону худшего исхода. Разное у инструментов только
СОДЕРЖИМОЕ разделов, поэтому рендер приходит параметром, а всё остальное —
отсюда.
"""

import datetime
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Отказ гейта вопросов PM печатается ВНУТРИ того же блока, который рецепт уже
# обязан копировать дословно. Текст отказа собирает гейт — здесь он только
# вкладывается: две редакции одного отказа разошлись бы молча.
import _polisade_pm_gate as pm_gate  # noqa: E402

PR_BODY_OPT = "--pr-body"

# Управляющие символы (в том числе перевод строки, возврат каретки и табуляция)
# в ОДНОСТРОЧНОЙ строке отчёта. Значение, пришедшее из имени файла или из
# frontmatter артефакта, может нести любой из них: перевод строки разрывает
# пункт списка на два (второй становится посторонней строкой в теле PR), а
# возврат каретки в терминале перерисовывает строку поверх уже напечатанной —
# то есть вердикт на экране расходится с вердиктом в данных. Это тот же класс,
# что и весь этот блок правок, только на символьном уровне.
#
# Диапазон включает и C1 (`\x80`–`\x9f`): `\x9b` — это CSI, односимвольная
# форма `ESC [`, и терминал, который её понимает, очистит строку ровно так же,
# как `\r`. Ограничиться ASCII-управлением значило бы закрыть одно написание
# того же приёма.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def one_line(value):
    """Значение как ОДНА строка: управляющие символы становятся пробелом.

    Общий контракт для всех отрисовщиков отчётов (тело PR и box doctor'а):
    строка, которую печатают PM'у, не имеет права выглядеть иначе, чем
    значение, из которого она собрана. Второй потребитель ЧИТАЕТ эту функцию,
    а не переписывает регулярку у себя.
    """
    return _CONTROL_RE.sub(" ", str(value))


def code_span(text):
    """Значение внутри markdown-кода, из которого оно не может «сбежать».

    Длина ограды выбирается по самой длинной цепочке обратных кавычек ВНУТРИ
    значения: имя файла с бэктиком — законное имя, а одинарная ограда на нём
    рвётся, и напечатанный адрес перестаёт быть адресом.
    """
    text = one_line(text)
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # Пробел по краям добавляется не только из-за бэктика: markdown СНИМАЕТ по
    # одному пробелу с каждого конца содержимого, и адрес ` a.md ` показался бы
    # как `a.md` — другое, тоже законное имя файла.
    pad = " " if text[:1] in ("`", " ") or text[-1:] in ("`", " ") else ""
    return "%s%s%s%s%s" % (fence, pad, text, pad, fence)


class PrBodyRefusal(Exception):
    """Путь для тела PR разобрать невозможно — это отказ, а не умолчание."""


def parse_pr_body_arg(argv):
    """`--pr-body=<путь>` → Path или None.

    Форма ровно одна, со знаком равенства, и это не вкусовщина: у мигратора
    позиционный аргумент ровно один — корень проекта, и он берётся как первый
    токен без `--`. Отдельное значение (`--pr-body body.md`) уехало бы в корень
    проекта, а сама опция осталась бы без пути. Поэтому пробельная форма —
    ОТКАЗ с названной заменой, а не догадка. `polisade_sync.py` разбирает argv
    через argparse и такой ловушки не имеет, но форма у двух рецептов обязана
    быть одна: рецепт пишут копированием.
    """
    path = None
    for token in argv:
        if not token.startswith(PR_BODY_OPT):
            continue
        rest = token[len(PR_BODY_OPT):]
        if not rest.startswith("=") or not rest[1:].strip():
            raise PrBodyRefusal(
                "Непонятная опция `%s`. Форма ровно одна: "
                "`--pr-body=<путь.md>` — со знаком равенства, потому что "
                "отдельное значение инструмент принял бы за корень проекта."
                % token)
        path = rest[1:]
    return Path(path) if path is not None else None


def bullets(items, empty="_Ни одного._"):
    return list(items) if items else [empty]


def questions_lines(questions, heading="### Вопросы PM"):
    """Раздел «вопросы PM» — общий у обоих инструментов.

    Строка вопроса называет АДРЕС артефакта, а не только его номер. Замерено
    на живом прогоне: тело PR печатало `- **001** — <инструкция>`, а путь у
    инструмента был (`pm_questions[].path`) и отбрасывался рендером. Ревьюер
    открывает такой PR на дифф в десятки файлов, и «001» по нему не ищется.

    Адрес НЕ придумывается: печатается то, что назвал сам инструмент — `path`,
    а для вопроса про пакет — `silo`. Если адреса нет ни в одном поле, вопрос
    всё равно печатается: его исчезновение и есть та тишина, ради которой
    раздел заведён.
    """
    # Материализуем СРАЗУ: итератор, исчерпанный циклом ниже, дал бы в
    # заголовке «(0)» под непустым списком — отчёт, противоречащий сам себе.
    questions = list(questions)
    rows = []
    for q in questions:
        ident = q.get("id") or q.get("kind") or "?"
        text = q.get("question") or q.get("detail") or ""
        if isinstance(text, (list, tuple)):
            text = " ".join(str(t) for t in text)
        head = "- **%s**" % one_line(ident)
        address = q.get("path") or q.get("silo")
        if address:
            head += " %s" % code_span(address)
        kind = q.get("kind")
        if kind and kind != ident:
            head += " (%s)" % one_line(kind)
        rows.append("%s — %s" % (head, one_line(text)))
    return ["%s (%d)" % (heading, len(questions))] + bullets(rows)


# ── Готовый ответ PM одним куском ──────────────────────────────────────────
# Отчёт доезжал до PM через пересказ модели. Замерено на четырёх живых
# прогонах мигратора: готовая строка `summary` напечатана дословно 1 раз из 4,
# маршрут целиком — 1 раз из 4, один раз он исчез вовсе, а ДВА раза был урезан
# в одних и тех же двух местах: модель сохранила имена команд и выбросила
# ровно те оговорки, ради которых маршрут писался. Правило «печатай ДОСЛОВНО»
# записано в рецепте трижды и не помогает — это самый слабый рычаг из
# замеренных; сильнейший — перенести вычисление в инструмент.
#
# Поэтому инструмент отдаёт не поля, которые модель сложит в ответ, а ГОТОВЫЙ
# БЛОК, который И ЕСТЬ ответ: собирать нечего, сокращать нечего.
_PM_BLOCK_RULE = "═" * 62
_PM_BLOCK_TITLE = "ОТЧЁТ ДЛЯ PM"
_PM_BLOCK_STEPS_HEAD = "Следующий шаг:"


def pm_block(payload):
    """Блок для PM из ТЕХ ЖЕ полей отчёта, либо None, если говорить нечего.

    Собирается ИЗ полей (`status`, `summary`, `next_steps`), а не пишется
    вторым текстом рядом: две копии одного факта расходятся молча и всегда в
    сторону худшего исхода.

    Блок намеренно короткий — статус, сводка, маршрут. Списки миграций и
    вопросов в него не входят: блок, выросший до тридцати строк, приглашает
    ровно то сокращение, ради которого он и заведён.

    Типы приводятся, а не предполагаются. Этот рендер — ДОПОЛНИТЕЛЬНЫЙ слой
    над обязательным: он вызывается ДО печати JSON-документа, и исключение в
    нём оставило бы stdout пустым, то есть ошибка представления превратилась
    бы в нарушение контракта «один JSON-документ». Строка вместо списка шагов
    считается ОДНИМ шагом: разобрать её по буквам было бы хуже, чем не
    разбирать вовсе.

    СОДЕРЖИМОЕ полей при этом не правится: строки печатаются как есть, и это
    не небрежность, а контракт — блок обязан нести `summary` и каждую строку
    `next_steps` БАЙТ-В-БАЙТ, иначе рушится то единственное, ради чего он
    заведён. Нормализация многострочного значения место имеет там, где
    носитель это требует (markdown тела PR), а не здесь.
    """
    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        summary = str(summary)
    raw_steps = payload.get("next_steps") or []
    if isinstance(raw_steps, str):
        steps = [raw_steps]
    elif isinstance(raw_steps, (list, tuple)):
        steps = [s if isinstance(s, str) else str(s) for s in raw_steps]
    else:
        # Не «шагов нет»: пустой маршрут рецепт читает как «режим выключен»,
        # то есть молчание здесь стало бы предметным утверждением, которого
        # никто не делал. Форма, которую нельзя прочитать, НАЗЫВАЕТСЯ.
        steps = ["Маршрут не прочитан: поле маршрута пришло в форме `%s`. "
                 "Это сбой отчёта, а не отсутствие следующего шага."
                 % type(raw_steps).__name__]
    gate = payload.get("pm_gate")
    # Метка «запись сделана изнутри агентской сессии» — ВНУТРИ блока и ДО
    # его нижней черты: блок целиком это единственное, что рецепт копирует
    # дословно, и строка, стоящая снаружи, повторила бы судьбу отчёта,
    # который пересказывают. Печатается независимо от того, остановлен
    # прогон или нет: помеченное признание гейт снимает — и именно тот
    # прогон, что закончился нулём, обязан назвать, кто его снял.
    session_lines = pm_gate.agent_session_lines(gate)
    # Помеченная запись — сама по себе повод собрать блок: отчёт без сводки и
    # без маршрута («говорить нечего») перестаёт быть таким, как только
    # решение за PM записал процесс, начатый инструментом.
    if not summary and not steps and not session_lines \
            and not pm_gate.blocked(gate):
        # «Говорить нечего» — не про заблокированный прогон. Проверка отказа
        # стояла НИЖЕ этого выхода, и прогон без сводки и маршрута уносил
        # отказ с собой: `blocked()` истинно, текст отказа есть, а блок —
        # None. Отказ обязан пережить пустоту всего остального: он и есть то
        # единственное, ради чего блок собирается.
        return None
    lines = [_PM_BLOCK_RULE, _PM_BLOCK_TITLE, _PM_BLOCK_RULE, ""]
    status = payload.get("status")
    if status:
        lines.append("Статус прогона: %s" % (status,))
    if summary:
        lines.append(summary)
    if steps:
        lines += ["", _PM_BLOCK_STEPS_HEAD]
        lines += ["%d. %s" % (i, step) for i, step in enumerate(steps, 1)]
    if session_lines:
        lines += [""] + session_lines
    lines += ["", _PM_BLOCK_RULE]
    if pm_gate.blocked(gate):
        # Отказ едет ВНУТРИ блока, а не рядом с ним: блок — единственное, что
        # рецепт обязан копировать целиком, и отдельный текст рядом повторил бы
        # судьбу отчёта, который пересказывают. Ниже маршрута, потому что он
        # отменяет всё, что маршрут предлагал сделать дальше.
        lines += [""] + pm_gate.refusal_lines(gate)
    return "\n".join(lines)


def paths_lines(payload):
    """Раздел «затронутые пути» — общий у обоих инструментов.

    Путь вне коммита НАЗВАН, а не выкинут: `stage_paths` уже, чем
    `touched_paths` (gitignored не стейджится), и молчаливое расхождение этих
    двух чисел в отчёте читалось бы как потерянный файл.
    """
    touched = payload.get("touched_paths") or []
    staged = set(payload.get("stage_paths") or [])
    return ["### Затронутые пути (%d, из них в коммит %d)"
            % (len(touched), len(staged))] + bullets(
        ["- `%s`%s" % (p, "" if p in staged else " — вне коммита (gitignore)")
         for p in touched])


class PrBodyRejected(Exception):
    """Цель не годится для записи — говорим это, а не пишем куда попало."""


# Файлы, поверх которых тело PR не ложится НИКОГДА, независимо от того, пишет
# ли их этот конкретный прогон. Список `touched_paths` закрывал только тот
# случай, когда состояние меняется: на полностью мигрированном проекте он пуст,
# и `--pr-body=.state/PROJECT_STATE.json` затирал состояние отчётом о нём же —
# экземпляр был починен, класс остался.
_ALWAYS_FORBIDDEN = (
    ".state/PROJECT_STATE.json",
    ".state/counters.json",
    ".state/knowledge.json",
    # Память гейта. Её не было в списке, потому что она пишется ВНУТРИ
    # `gate_verdict`, то есть позже расчёта `touched_paths` — и штатная опция
    # `--pr-body` укладывала отчёт поверх журнала. Следующий прогон читал не
    # JSON, второе плечо барьера слепло, и сделано это было своим же
    # инструментом, без обращения к файловой системе руками.
    ".state/pm-gate-ledger.json",
)

#: Каталоги, внутрь которых тело PR не ложится НИКОГДА — ни одним именем.
#: Запрет по ВХОЖДЕНИЮ, а не по списку файлов: `.git/HEAD`, `.git/config`,
#: `.git/index` — разные файлы с одним последствием, и перечислять их значило
#: бы догонять содержимое каталога, который ведёт не наш код. Замерено:
#: `--pr-body=<корень>/.git/HEAD` записывал markdown в HEAD, после чего
#: `git status` отвечал кодом 128, а `tree_version` возвращал мусор — то есть
#: одна опция ломала и репозиторий, и барьер решений.
_ALWAYS_FORBIDDEN_DIRS = (".git",)

#: Те же имена в нижнем регистре — для сравнения ПО ИМЕНИ компонента. Одного
#: сравнения по файлу мало: оно работает, только когда обе стороны существуют
#: и указывают в один inode, то есть не видит ни вложенный репозиторий
#: (`vendor/x/.git` — свой каталог, не наш корневой), ни несуществующую пока
#: цель. Одного сравнения по имени тоже мало: юникодная нормализация даёт
#: имя, которое выглядит иначе, а открывает тот же каталог. Нужны оба.
_ALWAYS_FORBIDDEN_DIR_NAMES = frozenset(d.lower() for d in _ALWAYS_FORBIDDEN_DIRS)


def _guarded_dir_hit(final, root_real) -> str:
    """Имя служебного каталога, внутрь которого целится путь, либо пусто.

    Первая редакция сравнивала ЛЕКСЕМЫ (`guarded in final.parents`) — и это
    была та же ошибка, которую соседняя `_hits_banned` объявляет измеренной:
    на регистронезависимой ФС `--pr-body=<корень>/.GIT/HEAD` не совпадал с
    `<корень>/.git` ни одной строкой, проходил проверку и затирал настоящий
    HEAD (замерено: `git status` отвечал кодом 128). Проверка была зелёной на
    своей же платформе, а тест ходил только по нижнему регистру — то есть
    соглашался сам с собой.

    Теперь решают ДВА признака на каждом компоненте пути: имя компонента без
    учёта регистра (ловит `.GIT`, `.Git` и вложенный `vendor/x/.git`, включая
    случай, когда цели ещё нет) и тождество файла (ловит то, что именем не
    видно, — юникодные варианты, ссылки). Вложенный репозиторий сторожится
    сознательно: тело PR не имеет права лечь в служебный каталог ЛЮБОГО
    репозитория, а не только нашего.
    """
    guarded = [root_real / d for d in _ALWAYS_FORBIDDEN_DIRS]
    for cand in (final, *final.parents):
        if cand.name.lower() in _ALWAYS_FORBIDDEN_DIR_NAMES:
            return cand.name
        for g in guarded:
            try:
                if cand.exists() and g.exists() and os.path.samefile(cand, g):
                    return g.name
            except OSError:
                continue
        if cand == root_real:
            break
    return ""


def _hits_banned(final, banned) -> bool:
    """Целится ли путь в запрещённый файл. Сравнение НЕ по написанию.

    Строковое равенство путей — проверка, зелёная на своей же платформе: на
    APFS (умолчание macOS) и на Windows файловая система регистр не различает,
    а `Path.resolve()` его не нормализует. Замерено: `--pr-body` с именем
    `.state/project_state.JSON` проходил проверку и затирал настоящий
    `PROJECT_STATE.json` — то есть штатная опция уничтожала файл, без которого
    отказывают все команды. То же делает юникодная нормализация (NFC/NFD).

    Поэтому решает ФАЙЛ, а не строка: когда обе стороны существуют, вердикт
    выносит `os.path.samefile` — он смотрит на устройство и inode, и регистр с
    нормализацией закрываются оба разом. Когда цели ещё нет, затирать нечего,
    и остаётся сравнение написания: оно ловит точное совпадение и не обещает
    большего.
    """
    for b in banned:
        if final == b:
            return True
        try:
            if final.exists() and b.exists() and os.path.samefile(final, b):
                return True
        except OSError:
            continue
    return False


def _vet_target(pr_body_path, root, forbidden):
    """Проверить цель ДО записи. Возвращает разрешённый путь либо бросает.

    Отказы закрывают каждый свой способ промахнуться:

    * ВЫХОД ЗА КОРЕНЬ ПРОЕКТА — по РАЗРЕШЁННОМУ пути родителя, а не по
      написанию. Это же закрывает ссылку в промежуточном компоненте:
      `.polisade/tmp`, указывающий наружу, пропускал запись мимо проекта
      (`is_symlink()` на файле внутри такой ссылки ложен), а `resolve()`
      приводит его к настоящему каталогу и тот не лежит под корнем. Обход
      КАЖДОГО компонента здесь был бы хуже: на macOS `/var` сам символическая
      ссылка, и любой путь во временном каталоге отвергался бы;
    * СИМЛИНК В САМОМ ФАЙЛЕ — отдельно, потому что ссылка внутри проекта,
      ведущая наружу, содержанию родителя не противоречит;
    * НЕ ОБЫЧНЫЙ ФАЙЛ. Каталог, устройство, FIFO: в последний случай можно
      направить и stdout, и тогда на один поток уедут markdown и JSON — то
      есть контракт «один JSON-документ» ломается записью тела;
    * ФАЙЛ, КОТОРЫЙ ПИШЕТ САМ ПРОГОН, и постоянный список состояния.

    ОТНОСИТЕЛЬНЫЙ путь разрешается от РАБОЧЕГО КАТАЛОГА, как его понимает
    оболочка, а не от корня проекта. Склейка с корнем давала `p/p/…`: рецепт
    передаёт `--pr-body={project_root}/.polisade/tmp/pr-body.md`, то есть путь
    УЖЕ содержит корень, и шаг 6 искал файл там, где его нет.
    """
    root_real = Path(root).resolve()
    target = Path(pr_body_path)
    try:
        if target.is_symlink():
            raise PrBodyRejected("цель — символическая ссылка")
        resolved_parent = target.parent.resolve()
        resolved_parent.relative_to(root_real)
    except ValueError:
        raise PrBodyRejected("путь ведёт за пределы проекта `%s`" % root_real)
    except (OSError, RuntimeError) as e:
        # Цикл ссылок и недоступный компонент — тоже отказ, а не traceback:
        # ответ обязан остаться одним JSON-документом.
        raise PrBodyRejected("путь не разрешается (%s)" % e.__class__.__name__)

    if target.exists() and not target.is_file():
        raise PrBodyRejected("цель существует и не является обычным файлом")

    final = resolved_parent / target.name
    hit = _guarded_dir_hit(final, root_real)
    if hit:
        raise PrBodyRejected(
            "путь ведёт внутрь служебного каталога `%s` — тело PR не "
            "ложится туда ни одним именем" % hit)
    banned = {root_real / p for p in _ALWAYS_FORBIDDEN}
    for p in forbidden or ():
        try:
            banned.add(Path(p).resolve())
        except (OSError, RuntimeError):
            continue
    if _hits_banned(final, banned):
        raise PrBodyRejected(
            "этот путь пишет сам прогон (или это файл состояния проекта) — "
            "тело PR затёрло бы его")
    return final


def skipped_note(pr_body_path, reason):
    """Опция была указана, файла нет — и это НАЗВАНО, а не пропало из ответа."""
    return {"path": str(pr_body_path), "status": "skipped", "detail": reason}


def write_pr_body(payload, pr_body_path, render, root=".", forbidden=()):
    """Записать тело PR и вернуть поле `pr_body` для ответа.

    Исход записи — ЧАСТЬ отчёта, а не тишина: рецепт обязан отличить «тело
    собрано» от «тела нет», иначе PR снова получит одну строку, и никто не
    узнает, почему (третья форма, issue #356).

    ЗАЯВЛЕННЫЙ ПРЕДЕЛ: между проверкой и записью остаётся окно, в которое
    разрешённый каталог можно подменить ссылкой. Закрыть его целиком значит
    открывать всё относительно дескриптора каталога (`openat`), чего этот
    модуль не делает: тело PR пишется в собственный проект вызывающего, где
    такой подмены не бывает, а сложность обошлась бы дороже закрываемого
    риска. Названо здесь, чтобы не выглядеть гарантией.
    """
    note = {"path": str(pr_body_path)}
    tmp_name = None
    try:
        target = _vet_target(pr_body_path, root, forbidden)
        target.parent.mkdir(parents=True, exist_ok=True)
        # `mkstemp` создаёт файл ЭКСКЛЮЗИВНО и с уникальным именем. Прежний
        # `<цель>.tmp` открывался обычной записью, поэтому заранее положенная
        # по этому имени ссылка уводила запись наружу, а параллельные прогоны
        # делили одно имя. `os.replace` затем меняет содержимое атомарно:
        # оборванная запись не оставляет половины отчёта в PR.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(render(payload))
        os.replace(tmp_name, str(target))
        tmp_name = None
        note["status"] = "written"
    # `Exception` намеренно шире прежнего перечня: сборка markdown идёт ДО
    # печати JSON-документа, и сбой отрисовщика (неожиданный тип в поле отчёта)
    # оставлял бы stdout пустым — то есть ошибка ТЕЛА PR ломала бы контракт
    # ОТЧЁТА. Третья форма для этого уже есть: `status: failed` с причиной.
    except (OSError, RuntimeError, PrBodyRejected, Exception) as e:
        reason = (str(e) if isinstance(e, PrBodyRejected)
                  else (getattr(e, "strerror", None) or str(e)
                        or e.__class__.__name__))
        note["status"] = "failed"
        note["detail"] = (
            "Тело PR не записано (%s). Скажи это в теле PR прямым текстом и "
            "процитируй отчёт из этого ответа; НЕ подменяй тело сообщением "
            "коммита молча." % reason)
    finally:
        if tmp_name:
            # Неудача не имеет права оставить в проекте мусор, который
            # остановит постапплайную сверку `git status` со `stage_paths`.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return note


def append_section(pr_body_path, lines, root="."):
    """Дописать раздел в УЖЕ написанное тело PR. Возвращает поле `pr_body`.

    Отсрочка вопроса PM записывается ПОСЛЕ прогона, который собрал тело PR
    (вопросы рождаются в прогоне, решение по ним принимается по его отказу), а
    обещание «отсрочка видна в теле PR» касается того же самого файла. Второй
    прогон его не перепишет: он уже `up_to_date` и напечатает «Миграций: 0» —
    ровно та тишина, ради которой опция `--pr-body` и заведена.

    Проверка цели — ТА ЖЕ (`_vet_target`), что и у записи: правило, записанное
    дважды, расходится молча. Отличие ровно одно и оно названо: дописывать
    можно только в существующий обычный файл; отсутствующая цель это
    `skipped`, а не новый файл с одним разделом вместо отчёта.
    """
    note = {"path": str(pr_body_path)}
    tmp_name = None
    try:
        target = _vet_target(pr_body_path, root, ())
        if not target.exists():
            return skipped_note(pr_body_path,
                                "тела PR по этому пути нет — дописывать не во "
                                "что; назови это в теле PR прямым текстом")
        existing = target.read_text(encoding="utf-8")
        body = existing if existing.endswith("\n") else existing + "\n"
        body += "\n" + "\n".join(lines) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp_name, str(target))
        tmp_name = None
        note["status"] = "appended"
    except Exception as e:                          # noqa: BLE001 — см. write_pr_body
        reason = (str(e) if isinstance(e, PrBodyRejected)
                  else (getattr(e, "strerror", None) or str(e)
                        or e.__class__.__name__))
        note["status"] = "failed"
        note["detail"] = (
            "Раздел не дописан в тело PR (%s). Скажи это в теле PR прямым "
            "текстом и процитируй отсрочку из этого ответа." % reason)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return note


# ── Полный отчёт для PM — файлом проекта (issue #402, решение PM 08.10) ────
# Дословной печати блока от слабой модели добиться не удалось ни одним
# рычагом: рендер в инструменте плюс запрет пересказа в постоянном контексте
# дали 0 прогонов из 5 «блок целиком». Числа и вердикт модель при этом
# передаёт верно почти всегда. Цель сменилась: полный блок лежит в ФАЙЛЕ
# проекта, а инструмент печатает путь к нему готовой строкой — PM открывает
# то, что напечатал инструмент, а не то, что пересказала модель.
#
# Байты файла — ТЕ ЖЕ, что напечатаны: функция ниже получает готовый текст и
# ничего в нём не меняет, а вызывающий печатает ту же переменную. Второго
# сборщика нет, и разойтись нечем.
#
# Каталог лежит под `.state/` и несёт СОБСТВЕННЫЙ `.gitignore` со звёздочкой:
# отчёт локален, как само состояние, и не появляется в `git status` даже в
# проекте, чей корневой `.gitignore` не закрывает `.state/*` (браунфилд,
# ручная правка). Без этого dry-run, который «ничего не пишет», оставлял бы
# неотслеживаемый файл, и постапплайная сверка `git status` со `stage_paths`
# останавливала бы рецепт коммита на ровном месте.
PM_REPORT_DIR = ".state/pm-reports"
#: Сколько отчётов одной команды хранится. Каждый прогон пишет НОВЫЙ файл —
#: предыдущий не затирается; старше этого числа удаляются, и это правило
#: названо в docs/config-reference.md, а не происходит молча.
PM_REPORT_KEEP = 20
_PM_REPORT_COMMANDS = ("migrate", "sync", "doctor")
_PM_REPORT_LATEST_VERSION = 1
_PM_REPORT_NAME_RE = re.compile(
    r"^(?P<cmd>[a-z]+)-(?P<stamp>\d{8}T\d{12}Z)-[0-9a-f]{6}\.(?:txt|md)$")
_PM_REPORT_IGNORE = "*\n"


class PmReportRejected(Exception):
    """Отчёт в файл не пишется — и это называется, а не проглатывается."""


def pm_report_latest_name(command):
    return "%s.latest.json" % command


#: Открывать всё относительно ДЕСКРИПТОРА каталога можно не везде: на Windows
#: нет ни `O_NOFOLLOW`, ни `dir_fd`. Там работает запасной путь по именам — с
#: теми же отказами (ссылка в компоненте, не каталог, выход за корень), но с
#: окном между проверкой и записью; это названо, а не выдано за гарантию.
_DIRFD_OK = (hasattr(os, "O_NOFOLLOW")
             and os.open in os.supports_dir_fd
             and os.stat in os.supports_dir_fd
             and os.mkdir in os.supports_dir_fd
             and os.link in os.supports_dir_fd
             and os.rename in os.supports_dir_fd
             and os.unlink in os.supports_dir_fd
             and os.listdir in os.supports_fd)

#: Временные файлы старше этого возраста — остатки оборванной записи. Каталог
#: скрыт собственным `.gitignore`, поэтому без подрезки они копились бы молча.
_PM_REPORT_TMP_MAX_AGE = 3600


class _ReportDir:
    """Каталог отчётов: операции по ИМЕНИ внутри него, без выхода наружу.

    Две реализации одного набора операций. На POSIX — дескриптор каталога и
    `O_NOFOLLOW` на каждом компоненте: подмена каталога ссылкой между проверкой
    и записью упирается в само открытие, отдельной проверки нет. Без `dir_fd`
    — абсолютные пути внутри разрешённого каталога, проверенного по `lstat`.
    """

    def __init__(self, root, create):
        self.fd = self.base_fd = None
        self.path = None
        parts = PM_REPORT_DIR.split("/")
        if _DIRFD_OK:
            self._open_fd(root, parts, create)
        else:
            self._open_path(root, parts, create)

    @staticmethod
    def _rejected_for(parts, i, st):
        if stat.S_ISLNK(st.st_mode):
            raise PmReportRejected(
                "`%s` — символическая ссылка; отчёт не пишется мимо проекта"
                % "/".join(parts[:i + 1]))
        if not stat.S_ISDIR(st.st_mode):
            raise PmReportRejected("`%s` не каталог" % "/".join(parts[:i + 1]))

    @staticmethod
    def _missing(parts, i, create):
        # `.state` не создаётся: каталога состояния нет — проект не
        # инициализирован, и класть туда отчёт значило бы выдать служебный
        # каталог за проект. Свой каталог — создаётся.
        if not create or i == 0:
            raise PmReportRejected(
                "каталога `%s` нет — проект не инициализирован"
                % "/".join(parts[:i + 1]))

    def _open_fd(self, root, parts, create):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
        try:
            self.base_fd = os.open(os.path.realpath(str(root)), flags)
        except OSError as exc:
            raise PmReportRejected("корень проекта не открывается (%s)"
                                   % (exc.strerror or exc.__class__.__name__))
        fd = self.base_fd
        try:
            for i, part in enumerate(parts):
                try:
                    st = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    self._missing(parts, i, create)
                    try:
                        os.mkdir(part, 0o777, dir_fd=fd)
                    except FileExistsError:
                        pass            # параллельный прогон успел первым
                    st = os.stat(part, dir_fd=fd, follow_symlinks=False)
                self._rejected_for(parts, i, st)
                nxt = os.open(part, flags, dir_fd=fd)
                if fd != self.base_fd:
                    os.close(fd)
                fd = nxt
            self.fd = fd
        except BaseException:
            if fd != self.base_fd:
                os.close(fd)
            self.close()
            raise

    def _open_path(self, root, parts, create):
        base = os.path.realpath(str(root))
        cur = base
        for i, part in enumerate(parts):
            p = os.path.join(cur, part)
            try:
                st = os.lstat(p)
            except FileNotFoundError:
                self._missing(parts, i, create)
                try:
                    os.mkdir(p)
                except FileExistsError:
                    pass
                st = os.lstat(p)
            self._rejected_for(parts, i, st)
            cur = p
        # Сравнение после `normcase`: на Windows `realpath` нормализует
        # регистр и короткие имена, и строгое равенство отвергало бы КАЖДУЮ
        # запись. Ссылки в компонентах уже отвергнуты `lstat` выше; здесь —
        # только «каталог лежит под корнем проекта».
        real = os.path.normcase(os.path.realpath(cur))
        if not real.startswith(os.path.normcase(base) + os.sep):
            raise PmReportRejected("каталог отчётов разрешается за пределы "
                                   "проекта")
        self.path = cur

    def close(self):
        for fd in (self.fd, self.base_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd = self.base_fd = None

    def _p(self, name):
        return os.path.join(self.path, name)

    def lstat(self, name):
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return os.lstat(self._p(name))

    def open(self, name, flags, mode=0o666):
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        if self.fd is not None:
            return os.open(name, flags, mode, dir_fd=self.fd)
        return os.open(self._p(name), flags, mode)

    def link_new(self, src, dst):
        """Опубликовать `src` под именем `dst`, которого ещё НЕТ."""
        if self.fd is not None:
            os.link(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                    follow_symlinks=False)
            return
        try:
            os.link(self._p(src), self._p(dst))
        except FileExistsError:
            raise
        except OSError:
            # Файловая система без жёстких ссылок. `rename` на Windows не
            # перезаписывает существующее имя — то же «только новое».
            if os.path.lexists(self._p(dst)):
                raise FileExistsError(dst)
            os.rename(self._p(src), self._p(dst))

    def replace(self, src, dst):
        if self.fd is not None:
            os.rename(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.replace(self._p(src), self._p(dst))

    def unlink(self, name):
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            os.unlink(self._p(name))

    def listdir(self):
        return os.listdir(self.fd if self.fd is not None else self.path)

    def read(self, name):
        fd = self.open(name, os.O_RDONLY)
        with os.fdopen(fd, "rb") as fh:
            return fh.read()


def _tmp_name(name):
    return ".%s.%s.tmp" % (name, secrets.token_hex(4))


def _write_tmp(d, data):
    tmp = None
    for _ in range(3):
        cand = _tmp_name("w")
        try:
            fd = d.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            continue
        tmp = cand
        break
    if tmp is None:
        raise FileExistsError("временное имя занято трижды подряд")
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            d.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp


def _write_exclusive(d, name, data):
    """Создать НОВЫЙ файл `name` целиком: временное имя, fsync, `link`.

    `O_EXCL|O_NOFOLLOW` на временном имени и публикация только под свободным
    итоговым: заранее положенная по любому из имён ссылка или файл — отказ, а
    не запись сквозь неё и не затирание. Читатель видит либо файл целиком,
    либо его отсутствие.
    """
    tmp = _write_tmp(d, data)
    try:
        d.link_new(tmp, name)
    finally:
        try:
            d.unlink(tmp)
        except OSError:
            pass


def _replace_file(d, name, data):
    """Заменить `name` целиком (указатель `*.latest.json`, `.gitignore`).

    `rename` не следует по ссылке в имени назначения — он заменяет саму
    запись каталога, так что ссылка на этом месте не уводит запись наружу.
    """
    tmp = _write_tmp(d, data)
    try:
        d.replace(tmp, name)
        tmp = None
    finally:
        if tmp:
            try:
                d.unlink(tmp)
            except OSError:
                pass


def _ensure_ignore(d):
    """`.gitignore` каталога — ОБЫЧНЫЙ файл ровно с `*`, иначе он чинится.

    Проверка «файл есть» была бы проверкой, зелёной на свежей фикстуре: git
    читает `.gitignore` только из обычного файла, так что ссылка на этом месте
    (в том числе битая) или чужое содержимое (`!migrate-*`, пустой файл)
    молча возвращали бы отчёты в `git status` — и постапплайная сверка рецепта
    коммита останавливалась бы на ровном месте. Поэтому решает тип и байты.
    """
    want = _PM_REPORT_IGNORE.encode("utf-8")
    try:
        st = d.lstat(".gitignore")
    except FileNotFoundError:
        try:
            _write_exclusive(d, ".gitignore", want)
            return
        except FileExistsError:
            st = d.lstat(".gitignore")     # параллельный прогон успел первым
    if stat.S_ISREG(st.st_mode):
        try:
            if d.read(".gitignore") == want:
                return
        except OSError:
            pass
    # Ссылка, каталог или чужие байты: заменяется сама запись каталога.
    # Каталог на этом месте `rename` не заменит — это отказ записи.
    _replace_file(d, ".gitignore", want)


def _prune_reports(d, command, now):
    """Удалить отчёты команды старше `PM_REPORT_KEEP` последних и протухшие
    временные файлы. Возвращает число удалённых отчётов."""
    entries = d.listdir()
    for n in entries:
        if n.startswith(".") and n.endswith(".tmp"):
            try:
                st = d.lstat(n)
                if stat.S_ISREG(st.st_mode) and \
                        now - st.st_mtime > _PM_REPORT_TMP_MAX_AGE:
                    d.unlink(n)
            except OSError:
                continue
    names = sorted(
        (m.group("stamp"), n) for n in entries
        for m in [_PM_REPORT_NAME_RE.match(n)]
        if m and m.group("cmd") == command)
    removed = 0
    for _stamp, name in names[:max(0, len(names) - PM_REPORT_KEEP)]:
        try:
            if stat.S_ISREG(d.lstat(name).st_mode):
                d.unlink(name)
                removed += 1
        except OSError:
            continue
    return removed


def pm_report_line(note):
    """Готовая строка о файле отчёта — её печатает инструмент, а не модель."""
    if note.get("status") == "written":
        return "Полный отчёт для PM сохранён в файл: %s" % note["path"]
    return ("Полный отчёт для PM в файл НЕ сохранён (%s) — блок выше "
            "единственная его копия." % note.get("detail", "причина не названа"))


def emit_pm_text(stream, data):
    """Напечатать ТЕ ЖЕ байты, что легли в файл, а не их перекодировку.

    `stream.write(text)` кодирует кодировкой потока (на не-UTF-8 локали — с
    заменой символов), и «байты файла == напечатанному» держалось бы только
    при UTF-8. Поэтому печатаются байты, когда у потока есть буфер; без него —
    текст, и это единственный случай, где равенство не обещается.
    """
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        stream.flush()
        buf.write(data)
        buf.flush()
    else:
        stream.write(data.decode("utf-8"))
        stream.flush()


def save_pm_report(root, command, text, facts=None, ext="txt"):
    """Записать полный отчёт для PM файлом проекта. Возвращает поле `pm_report`.

    `text` — РОВНО то, что вызывающий печатает (с завершающим переводом
    строки, если он его печатает): функция не добавляет и не отрезает ничего,
    а напечатать вызывающий обязан байты `text.encode("utf-8")` через
    `emit_pm_text`. Имя у каждого прогона своё (`<команда>-<время UTC>-
    <случайный суффикс>`), поэтому следующий прогон той же команды не теряет
    предыдущий. Рядом обновляется `<команда>.latest.json` — указатель на
    последний отчёт плюс `facts`: то, что PM обязан увидеть и что следующая
    команда читает из состояния, а не из пересказа (маршрут, сводка, статус).

    Никогда не бросает: отчёт файлом — ДОПОЛНИТЕЛЬНЫЙ слой, и его сбой не
    имеет права унести с собой обязательный JSON-документ. Сбой НАЗЫВАЕТСЯ —
    `status: failed|skipped` с причиной и готовой строкой.

    ЗАЯВЛЕННЫЙ ПРЕДЕЛ запасного пути (платформа без `dir_fd`, то есть Windows):
    между проверкой компонентов и записью остаётся окно, в которое каталог
    можно подменить ссылкой. На POSIX этого окна нет.
    """
    note = {"status": "failed"}
    if command not in _PM_REPORT_COMMANDS:
        note["detail"] = "неизвестная команда отчёта `%s`" % command
        note["line"] = pm_report_line(note)
        return note
    now = datetime.datetime.now(datetime.timezone.utc)
    name = "%s-%s-%s.%s" % (command, now.strftime("%Y%m%dT%H%M%S%fZ"),
                            secrets.token_hex(3), ext)
    rel = "%s/%s" % (PM_REPORT_DIR, name)
    d = None
    try:
        data = text.encode("utf-8")
        d = _ReportDir(root, create=True)
        _ensure_ignore(d)
        _write_exclusive(d, name, data)
        digest = hashlib.sha256(data).hexdigest()
        note = {"status": "written", "path": rel, "sha256": digest}
        latest = {"version": _PM_REPORT_LATEST_VERSION, "command": command,
                  "path": rel, "sha256": digest,
                  "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        latest.update(facts or {})
        try:
            _replace_file(d, pm_report_latest_name(command),
                          (json.dumps(latest, indent=2, ensure_ascii=False)
                           + "\n").encode("utf-8"))
        except Exception as exc:                    # noqa: BLE001
            # Сам отчёт записан — это главное; несохранённый указатель
            # называется отдельно, а не переворачивает исход.
            note["latest_error"] = exc.__class__.__name__
        try:
            removed = _prune_reports(d, command, now.timestamp())
        except OSError:
            removed = 0
        if removed:
            note["pruned"] = removed
    except PmReportRejected as exc:
        status = "skipped" if "не инициализирован" in str(exc) else "failed"
        note = {"status": status, "detail": str(exc)}
    except Exception as exc:                        # noqa: BLE001 — см. выше
        note = {"status": "failed",
                "detail": getattr(exc, "strerror", None) or str(exc)
                or exc.__class__.__name__}
    finally:
        if d is not None:
            d.close()
    note["line"] = pm_report_line(note)
    return note


def read_latest_pm_report(root, command):
    """`(указатель, ошибка)` последнего отчёта команды. Никогда не бросает.

    Читатель для СЛЕДУЮЩЕЙ команды: маршрут и сводку прогона она берёт
    отсюда, а не из того, что модель сказала PM. Сверяет и сам файл отчёта:
    отчёт, который пропал или чьи байты не совпали с записанным хешем, — это
    названная ошибка, а не «отчёта не было». Перехват широкий, как у
    писателя: проверка doctor'а, упавшая traceback'ом, сломала бы его вывод.
    """
    d = None
    try:
        try:
            d = _ReportDir(root, create=False)
        except PmReportRejected as exc:
            if "не инициализирован" in str(exc):
                return None, None
            return None, str(exc)
        try:
            raw = d.read(pm_report_latest_name(command))
        except FileNotFoundError:
            return None, None
        latest = json.loads(raw.decode("utf-8"))
        if not isinstance(latest, dict) or not isinstance(
                latest.get("path"), str):
            return None, "указатель `%s` не имеет формы отчёта" \
                % pm_report_latest_name(command)
        name = latest["path"].rsplit("/", 1)[-1]
        if not _PM_REPORT_NAME_RE.match(name) or \
                latest["path"] != "%s/%s" % (PM_REPORT_DIR, name):
            return latest, "указатель называет чужой путь `%s`" % latest["path"]
        try:
            body = d.read(name)
        except FileNotFoundError:
            return latest, "файла отчёта `%s` нет" % latest["path"]
        if hashlib.sha256(body).hexdigest() != latest.get("sha256"):
            return latest, "байты отчёта `%s` не совпали с записанными" \
                % latest["path"]
        return latest, None
    except Exception as exc:                        # noqa: BLE001 — см. выше
        return None, "отчёт не прочитан (%s)" % exc.__class__.__name__
    finally:
        if d is not None:
            d.close()


def _report_facts(payload):
    """Что из прогона PM обязан увидеть — в указатель последнего отчёта.

    Ровно то, что раньше жило ТОЛЬКО в напечатанном тексте: статус, сводка с
    числами и маршрут. Вопросы PM и их отсрочки здесь не дублируются — они уже
    лежат в журнале гейта (`.state/pm-gate-ledger.json`), и вторая копия
    разошлась бы с ним молча; повторяется лишь статус гейта этого прогона.
    """
    raw_steps = payload.get("next_steps") or []
    steps = ([raw_steps] if isinstance(raw_steps, str)
             else [str(s) for s in raw_steps]
             if isinstance(raw_steps, (list, tuple)) else [])
    facts = {"status": payload.get("status"),
             "summary": payload.get("summary"),
             "next_steps": steps}
    gate = payload.get("pm_gate")
    if isinstance(gate, dict) and gate.get("status"):
        facts["pmGateStatus"] = gate.get("status")
    return facts


def emit_report(payload, pr_body_path, render, root=".", forbidden=(),
                command=None):
    """Один JSON-документ на stdout (OPS-108), блок для PM — на stderr и файлом.

    Контракт stdout не трогается: его читает `json.loads`, и вторая порция
    текста там сломала бы рецепт. Блок уходит туда же, куда и остальные
    PM-адресованные сообщения обоих инструментов, — на stderr, и тем же
    значением попадает в поле `pm_block`. Один вызов `pm_block()`, три точки
    выдачи (поле, stderr, файл `.state/pm-reports/`): разойтись им нечем —
    файл и stderr получают ОДНУ переменную. Путь к файлу печатается готовой
    строкой после блока и лежит в поле `pm_report` (issue #402).
    """
    if pr_body_path is not None:
        payload = dict(payload, pr_body=write_pr_body(
            payload, pr_body_path, render, root=root, forbidden=forbidden))
    # Сборка блока НЕ имеет права утащить за собой обязательный документ:
    # исключение здесь оставило бы stdout пустым, и рецепт остановился бы на
    # `json.loads`. Отказ называется на stderr — молчание читалось бы как
    # «блока не было».
    try:
        block = pm_block(payload)
    except Exception as exc:                       # noqa: BLE001 — см. выше
        block = None
        # Сбой сборки НАЗВАН и в машинном ответе: без поля «блок не собрался»
        # неотличимо от «блока и не было» — а это разные вещи для того, кто
        # читает отчёт программой.
        payload = dict(payload, pm_block_error=exc.__class__.__name__)
        print("Блок отчёта для PM не собран (%s) — ниже только JSON-документ."
              % exc.__class__.__name__, file=sys.stderr)
    printed = None
    report = None
    if block is not None:
        payload = dict(payload, pm_block=block)
        # ОДНА переменная на печать и на файл: байты файла — это ровно то, что
        # уходит на stderr, включая завершающий перевод строки `print`.
        printed = "%s\n" % (block,)
        if command is not None:
            report = save_pm_report(root, command, printed,
                                    facts=_report_facts(payload))
            payload = dict(payload, pm_report=report)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    sys.stdout.flush()
    if printed is not None:
        # ПОСЛЕ документа: в терминале PM видит блок последним, ближе всего к
        # ответу, который из него собирается. Печатаются ТЕ ЖЕ байты, что
        # легли в файл, — не их перекодировка потоком.
        emit_pm_text(sys.stderr, printed.encode("utf-8"))
        if report is not None:
            print(report["line"], file=sys.stderr)
            sys.stderr.flush()
