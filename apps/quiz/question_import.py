"""
Сұрақтарды Excel (.xlsx) файлынан импорттау және бос үлгі файлын жасау.

Файлда екі парақ:
- «Сұрақтар» — бір жол = бір сұрақ: тақырыпша, деңгей, тіл, контекст кілті,
  мәтін, код, A–D нұсқалары, дұрыс жауап әрпі;
- «Контексттер» (міндетті емес) — кілт, атауы, тіл, мәтін, код.
Бағандар тақырыбы бойынша емес, реті бойынша оқылады (1-жол — тақырыптар).

Тексеру «бәрі немесе ештеңе»: бір қате болса — ештеңе сақталмайды, қателер
жол нөмірімен қайтарылады. Пәнде бұрыннан бар сұрақ (мәтіні, тілі, тақырыпшасы
бірдей) қайта қосылмайды. Суреттер импортталмайды — оларды форма арқылы қосады.
"""

import zipfile
from io import BytesIO

from django.db import transaction
from django.utils.translation import gettext as _
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.datavalidation import DataValidation

from .constants import ANSWERS_PER_QUESTION, IMPORT_MAX_ROWS, QUESTIONS_PER_CONTEXT
from .models import Answer, Context, Language, Level, Question, Subtopic
from .services import ANSWER_LETTERS

# Excel (.xlsx) файлының MIME түрі
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Парақ атаулары аударылмайды: импорт оларды осы атаумен іздейді
QUESTIONS_SHEET = "Сұрақтар"
CONTEXTS_SHEET = "Контексттер"

# Бағандар реті (0-ден): «Сұрақтар» парағы
SUBTOPIC, LEVEL, LANGUAGE, CONTEXT, TEXT, CODE = range(6)
FIRST_ANSWER = 6  # A–D нұсқалары: 6, 7, 8, 9
CORRECT = FIRST_ANSWER + ANSWERS_PER_QUESTION
QUESTION_WIDTH = CORRECT + 1

# «Контексттер» парағы
CONTEXT_KEY, CONTEXT_TITLE, CONTEXT_LANGUAGE, CONTEXT_TEXT, CONTEXT_CODE = range(5)
CONTEXT_WIDTH = 5

# Кириллицаның А, В, С, Д әріптері латынша сияқты көрінеді — оларды латыншаға ауыстырамыз
CYRILLIC_TO_LATIN = str.maketrans({"А": "A", "В": "B", "С": "C", "Д": "D"})

# Тіл бағанында рұқсат етілген жазулар
LANGUAGE_ALIASES = {
    "kk": Language.KK,
    "kz": Language.KK,
    "қаз": Language.KK,
    "қазақша": Language.KK,
    "ru": Language.RU,
    "рус": Language.RU,
    "орыс": Language.RU,
    "орысша": Language.RU,
}

# Excel-де тізімнен таңдау (Деректерді тексеру) қолданылатын жол саны
VALIDATION_ROWS = IMPORT_MAX_ROWS + 1


def question_headers():
    """«Сұрақтар» парағының баған тақырыптары (интерфейс тілінде)."""
    return [
        _("Тақырыпша (01–20)"),
        _("Деңгей (A/B/C)"),
        _("Тіл (kk/ru)"),
        _("Контекст кілті"),
        _("Сұрақ мәтіні"),
        _("Код"),
        *ANSWER_LETTERS[:ANSWERS_PER_QUESTION],
        _("Дұрыс жауап (A–D)"),
    ]


def context_headers():
    """«Контексттер» парағының баған тақырыптары."""
    return [_("Кілт"), _("Атауы"), _("Тіл (kk/ru)"), _("Мәтіні"), _("Код")]


# ---------- Ұяшықтарды оқу ----------


def cell_text(value):
    """Ұяшық мәні мәтін ретінде: бос — "", 3.0 сияқты бүтін сан — "3"."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace("\r\n", "\n")


def code_text(value):
    """Код пен жауап нұсқасы: шегіністер сақталады, тек бос орындар болса — ""."""
    text = cell_text(value)
    return text if text.strip() else ""


def parse_number(value):
    """Тақырыпша нөмірі: 3, "03", 3.0 → 3; сан болмаса — None."""
    text = cell_text(value).strip()
    return int(text) if text.isdigit() else None


def parse_letter(value):
    """Деңгей мен дұрыс жауап әрпі: бас әріп, кириллица латыншаға ауыстырылады."""
    return cell_text(value).strip().upper().translate(CYRILLIC_TO_LATIN)


def parse_language(value):
    """Тіл: kk / ru (қаз, орыс т.б. да болады); танылмаса — None."""
    return LANGUAGE_ALIASES.get(cell_text(value).strip().lower())


class TooManyRows(Exception):
    """Парақта IMPORT_MAX_ROWS-тан көп жол бар."""


def read_rows(sheet, width):
    """
    Парақтың деректер жолдары (1-жол — тақырыптар), бос жолдарсыз:
    [(жол нөмірі, [ұяшық мәндері]), ...]. Әр жолда дәл width ұяшық.
    """
    rows = []
    cells = sheet.iter_rows(min_row=2, max_col=width, values_only=True)
    for number, values in enumerate(cells, start=2):
        values = list(values) + [None] * (width - len(values))
        if all(cell_text(value).strip() == "" for value in values):
            continue
        if len(rows) >= IMPORT_MAX_ROWS:
            raise TooManyRows
        rows.append((number, values))
    return rows


# ---------- Импорт ----------


def new_result():
    return {"errors": [], "questions": 0, "contexts": 0, "skipped": []}


def normalize_text(text):
    """Қайталануды салыстыру үшін: жол соңы бірдей, шеттегі бос орындарсыз."""
    return text.replace("\r\n", "\n").strip()


def import_questions(subject, file):
    """
    Excel файлындағы сұрақтар мен контексттерді пәнге қосады.

    Қайтарады: {"errors": [...], "questions": қосылған сұрақтар саны,
    "contexts": қосылған контексттер саны, "skipped": өткізілген жолдар нөмірі}.
    errors бос болмаса — дерекқорға ештеңе жазылмаған.
    """
    result = new_result()
    errors = result["errors"]

    try:
        workbook = load_workbook(file, read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, ValueError, OSError):
        errors.append(_("Файл оқылмады: ол Excel (.xlsx) файлы болуы керек."))
        return result

    try:
        if QUESTIONS_SHEET in workbook.sheetnames:
            question_sheet = workbook[QUESTIONS_SHEET]
        else:
            # Өз файлы болса: «Сұрақтар» парағы жоқ — бірінші парақ
            question_sheet = workbook.worksheets[0]
        question_rows = read_rows(question_sheet, QUESTION_WIDTH)
        context_rows = []
        if CONTEXTS_SHEET in workbook.sheetnames and question_sheet.title != CONTEXTS_SHEET:
            context_rows = read_rows(workbook[CONTEXTS_SHEET], CONTEXT_WIDTH)
    except TooManyRows:
        errors.append(
            _("Файлда жол тым көп: бір парақта ең көбі %(count)s жол.")
            % {"count": IMPORT_MAX_ROWS}
        )
        return result
    finally:
        workbook.close()

    def add_error(sheet, row, column, message):
        errors.append(
            _("«%(sheet)s» парағы, %(row)s-жол, «%(column)s»: %(message)s")
            % {"sheet": sheet, "row": row, "column": column, "message": message}
        )

    contexts = read_contexts(subject, context_rows, add_error)
    questions = read_questions(subject, question_rows, contexts, add_error, result)

    # Контекстте 5-тен көп белсенді сұрақ болмайды (бұрынғыларымен бірге)
    for key, data in contexts.items():
        total = data["existing_count"] + data["new_count"]
        if total > QUESTIONS_PER_CONTEXT:
            add_error(
                CONTEXTS_SHEET,
                data["row"],
                context_headers()[CONTEXT_KEY],
                _("«%(key)s» контекстіне %(total)s сұрақ байланады, ең көбі %(max)s.")
                % {"key": key, "total": total, "max": QUESTIONS_PER_CONTEXT},
            )

    if not errors and not questions and not result["skipped"] and not contexts:
        errors.append(_("Файлда сұрақ жоқ."))
    if errors:
        result["skipped"] = []
        return result

    save_import(subject, contexts, questions, result)
    return result


def read_contexts(subject, rows, add_error):
    """
    «Контексттер» парағы: {кілт: {...}}. Пәнде атауы, тілі, мәтіні бірдей контекст
    бұрыннан бар болса — жаңасы жасалмайды, сұрақтар соған байланады.
    """
    headers = context_headers()
    existing = {}
    for quiz_context in Context.objects.filter(subject=subject):
        key = (
            normalize_text(quiz_context.title),
            quiz_context.language,
            normalize_text(quiz_context.text),
        )
        existing.setdefault(key, quiz_context)

    contexts = {}
    for row, cells in rows:
        key = cell_text(cells[CONTEXT_KEY]).strip()
        title = cell_text(cells[CONTEXT_TITLE]).strip()
        language = parse_language(cells[CONTEXT_LANGUAGE])
        text = cell_text(cells[CONTEXT_TEXT]).strip()

        if not key:
            add_error(CONTEXTS_SHEET, row, headers[CONTEXT_KEY], _("бос болмауы керек."))
            continue
        if key in contexts:
            add_error(
                CONTEXTS_SHEET,
                row,
                headers[CONTEXT_KEY],
                _("«%(key)s» кілті %(row)s-жолда бар.")
                % {"key": key, "row": contexts[key]["row"]},
            )
            continue
        if not title:
            add_error(CONTEXTS_SHEET, row, headers[CONTEXT_TITLE], _("бос болмауы керек."))
        if language is None:
            add_error(CONTEXTS_SHEET, row, headers[CONTEXT_LANGUAGE], _("kk немесе ru болуы керек."))
        if not text:
            add_error(CONTEXTS_SHEET, row, headers[CONTEXT_TEXT], _("бос болмауы керек."))

        found = existing.get((normalize_text(title), language, normalize_text(text)))
        contexts[key] = {
            "row": row,
            "title": title,
            "language": language,
            "text": text,
            "code": code_text(cells[CONTEXT_CODE]),
            "instance": found,
            "existing_count": found.active_questions_count() if found else 0,
            "new_count": 0,
        }
    return contexts


def read_questions(subject, rows, contexts, add_error, result):
    """
    «Сұрақтар» парағы: сақталатын сұрақтар тізімі. Қателер add_error арқылы
    жиналады, бұрыннан бар сұрақтардың жол нөмірі result["skipped"]-ке жазылады.
    """
    headers = question_headers()
    subtopics = {
        subtopic.number: subtopic
        for subtopic in Subtopic.objects.filter(topic__subject=subject)
    }
    seen = {
        (normalize_text(text), language, subtopic_id)
        for text, language, subtopic_id in Question.objects.filter(
            subtopic__topic__subject=subject
        ).values_list("text", "language", "subtopic_id")
    }
    sheet = QUESTIONS_SHEET
    letters = ANSWER_LETTERS[:ANSWERS_PER_QUESTION]

    questions = []
    for row, cells in rows:
        errors_before = len(result["errors"])

        subtopic = subtopics.get(parse_number(cells[SUBTOPIC]))
        if subtopic is None:
            add_error(
                sheet,
                row,
                headers[SUBTOPIC],
                _("пәнде мұндай тақырыпша жоқ (01–%(max)s).") % {"max": len(subtopics)},
            )
        level = parse_letter(cells[LEVEL])
        if level not in Level.values:
            add_error(sheet, row, headers[LEVEL], _("A, B немесе C болуы керек."))
        language = parse_language(cells[LANGUAGE])
        if language is None:
            add_error(sheet, row, headers[LANGUAGE], _("kk немесе ru болуы керек."))
        text = cell_text(cells[TEXT]).strip()
        if not text:
            add_error(sheet, row, headers[TEXT], _("бос болмауы керек."))

        answers = [code_text(cells[FIRST_ANSWER + index]) for index in range(len(letters))]
        for letter, answer in zip(letters, answers):
            if not answer:
                add_error(sheet, row, letter, _("жауап нұсқасы бос болмауы керек."))
        correct = parse_letter(cells[CORRECT])
        if correct not in letters:
            add_error(
                sheet,
                row,
                headers[CORRECT],
                _("%(letters)s әріптерінің бірі болуы керек.") % {"letters": ", ".join(letters)},
            )

        context_key = cell_text(cells[CONTEXT]).strip()
        context = None
        if context_key:
            context = contexts.get(context_key)
            if context is None:
                add_error(
                    sheet,
                    row,
                    headers[CONTEXT],
                    _("«%(key)s» кілті «%(contexts)s» парағында жоқ.")
                    % {"key": context_key, "contexts": CONTEXTS_SHEET},
                )
            elif language and context["language"] and context["language"] != language:
                add_error(
                    sheet,
                    row,
                    headers[CONTEXT],
                    _("контекст пен сұрақтың тілі бірдей болуы керек."),
                )

        if len(result["errors"]) > errors_before:
            continue

        # Пәнде (немесе файлдың жоғарғы жолдарында) бұрыннан бар сұрақ — өткізіледі
        duplicate_key = (normalize_text(text), language, subtopic.pk)
        if duplicate_key in seen:
            result["skipped"].append(row)
            continue
        seen.add(duplicate_key)

        if context is not None:
            context["new_count"] += 1
        questions.append(
            {
                "subtopic": subtopic,
                "level": level,
                "language": language,
                "context_key": context_key,
                "text": text,
                "code": code_text(cells[CODE]),
                "answers": answers,
                "correct_index": letters.index(correct),
            }
        )
    return questions


@transaction.atomic
def save_import(subject, contexts, questions, result):
    """Тексерілген контексттер мен сұрақтарды бір транзакцияда сақтайды."""
    for data in contexts.values():
        if data["instance"] is None:
            data["instance"] = Context.objects.create(
                subject=subject,
                language=data["language"],
                title=data["title"],
                text=data["text"],
                code=data["code"],
            )
            result["contexts"] += 1

    for data in questions:
        context = contexts[data["context_key"]]["instance"] if data["context_key"] else None
        question = Question.objects.create(
            subtopic=data["subtopic"],
            context=context,
            language=data["language"],
            text=data["text"],
            code=data["code"],
            level=data["level"],
        )
        Answer.objects.bulk_create(
            Answer(question=question, text=text, is_correct=index == data["correct_index"])
            for index, text in enumerate(data["answers"])
        )
        result["questions"] += 1


def result_message(result):
    """Сәтті импорттан кейінгі хабарлама."""
    message = _("Қосылды: сұрақтар — %(questions)s, контексттер — %(contexts)s.") % {
        "questions": result["questions"],
        "contexts": result["contexts"],
    }
    if result["skipped"]:
        rows = ", ".join(str(row) for row in result["skipped"])
        message += " " + _("Банкте бұрыннан бар, өткізілді — жолдар: %(rows)s.") % {"rows": rows}
    return message


# ---------- Үлгі файл ----------


def fill_sheet(sheet, headers, widths):
    """Тақырып жолы (қалың), бағандар ені, тақырып жолы бекітіледі."""
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for index, width in enumerate(widths):
        sheet.column_dimensions[chr(ord("A") + index)].width = width
    sheet.freeze_panes = "A2"


def add_choice(sheet, column, choices):
    """Бағанда тек тізімдегі мән таңдалады (Excel-дің «Деректерді тексеру»)."""
    validation = DataValidation(
        type="list", formula1='"{}"'.format(",".join(choices)), allow_blank=True
    )
    sheet.add_data_validation(validation)
    validation.add(f"{column}2:{column}{VALIDATION_ROWS}")


def import_template(subject):
    """Бос үлгі файл (.xlsx, bytes): екі парақ, нұсқаулық және пәннің тақырыпшалары."""
    workbook = Workbook()
    letters = ANSWER_LETTERS[:ANSWERS_PER_QUESTION]

    questions = workbook.active
    questions.title = QUESTIONS_SHEET
    fill_sheet(questions, question_headers(), [12, 10, 9, 12, 50, 30, 20, 20, 20, 20, 12])
    add_choice(questions, "B", Level.values)
    add_choice(questions, "C", Language.values)
    add_choice(questions, chr(ord("A") + CORRECT), letters)

    contexts = workbook.create_sheet(CONTEXTS_SHEET)
    fill_sheet(contexts, context_headers(), [10, 30, 9, 60, 30])
    add_choice(contexts, "C", Language.values)

    guide = workbook.create_sheet(_("Нұсқаулық"))
    guide.column_dimensions["A"].width = 14
    guide.column_dimensions["B"].width = 40
    guide.column_dimensions["C"].width = 60
    lines = [
        [_("Сұрақтарды импорттау: %(subject)s") % {"subject": subject.name}],
        [],
        [_("«%(sheet)s» парағы: бір жол — бір сұрақ.") % {"sheet": QUESTIONS_SHEET}],
        [_("Тақырыпша, деңгей, тіл, мәтін, 4 нұсқа және дұрыс жауап — міндетті.")],
        [_("Контекстік сұрақта «Контекст кілті» бағанына контекстің кілтін жазыңыз (мысалы, K1).")],
        [
            _("«%(sheet)s» парағы: кілт, атауы, тіл, мәтін, код. Бір контекстке ең көбі %(max)s сұрақ.")
            % {"sheet": CONTEXTS_SHEET, "max": QUESTIONS_PER_CONTEXT}
        ],
        [_("Суреттер импортталмайды: суреті бар сұрақты импорттан кейін форма арқылы толықтырыңыз.")],
        [_("Қате болса, ештеңе сақталмайды. Банкте бұрыннан бар сұрақ қайта қосылмайды.")],
        [],
        [_("Мысал:")],
        question_headers(),
        ["03", "B", "kk", "", "Алғашқы ЭЕМ қалай аталады?", "", "ENIAC", "IBM PC", "Apple I", "Altair", "A"],
        [],
        [_("Пәннің тақырыпшалары:")],
        [_("Тақырыпша"), _("Тақырып"), _("Атауы")],
    ]
    for line in lines:
        guide.append(line)
    for subtopic in Subtopic.objects.filter(topic__subject=subject).select_related("topic"):
        guide.append([f"{subtopic.number:02d}", str(subtopic.topic), subtopic.name])

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
