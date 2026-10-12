"""
Студенттерді Excel (.xlsx) файлынан импорттау (тек әкімші) және кіру кодтары.

Файлдың бірінші парағы, бағандары: Аты-жөні, Топ, ИИН (1-жол — тақырыптар).
Студенттің логині — ИИН, құпия сөзі — импортта жасалатын кездейсоқ код.
Тексеру «бәрі немесе ештеңе»: бір қате болса — ештеңе сақталмайды.
ИИН бұрыннан бар болса, студенттің аты-жөні мен тобы жаңартылады, коды өзгермейді.
Жаңа кодтар тек бір рет — жүктелетін Excel файлында көрсетіледі (дерекқорда хэші ғана).
"""

import secrets
import string
import zipfile
from io import BytesIO

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import transaction
from django.utils.translation import gettext as _
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils.exceptions import InvalidFileException

from apps.quiz.constants import IMPORT_MAX_ROWS
from apps.quiz.question_import import TooManyRows, cell_text, read_rows

from .hashers import AccessCodeHasher
from .models import StudyGroup

# Бағандар реті (0-ден)
NAME, GROUP, IIN = range(3)
WIDTH = 3

IIN_LENGTH = 12
# Кіру коды — тек цифрлар: телефонда теру оңай, әріптердің үлкен-кішісі шатаспайды
CODE_LENGTH = 8


def column_headers():
    return [_("Аты-жөні"), _("Топ"), _("ИИН")]


def iin_is_valid(iin):
    """
    ИИН: 12 цифр, соңғысы — бақылау цифры. Алдымен 1–11 салмақтарымен есептеледі;
    қалдық 10 болса — 3–11, 1, 2 салмақтарымен; тағы 10 болса — ИИН қате.
    """
    if len(iin) != IIN_LENGTH or not iin.isdigit():
        return False
    digits = [int(digit) for digit in iin]
    for weights in (range(1, 12), [3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2]):
        control = sum(digit * weight for digit, weight in zip(digits, weights)) % 11
        if control != 10:
            return control == digits[11]
    return False


def normalize_iin(value):
    """ИИН ұяшығы: бос орындарсыз; Excel санды сақтағанда жоғалған бас нөлдер қайтарылады."""
    if isinstance(value, (int, float)):
        return cell_text(value).zfill(IIN_LENGTH)
    return "".join(cell_text(value).split())


def split_name(full_name):
    """«Сапарова Айгерім Нұрланқызы» → (тегі «Сапарова», аты «Айгерім Нұрланқызы»)."""
    parts = full_name.split()
    return parts[0], " ".join(parts[1:])


def generate_code():
    return "".join(secrets.choice(string.digits) for _index in range(CODE_LENGTH))


def set_access_code(user):
    """Студентке жаңа код қояды (хэші ғана сақталады) және кодтың өзін қайтарады."""
    code = generate_code()
    user.password = make_password(code, hasher=AccessCodeHasher.algorithm)
    return code


# ---------- Импорт ----------


def import_students(file):
    """
    Excel файлындағы студенттерді қосады немесе жаңартады.

    Қайтарады: {"errors": [...], "created": n, "updated": n, "rows": [{"name", "group",
    "iin", "code"}, ...]} — code тек жаңа студентте (бұрынғысында None).
    errors бос болмаса — дерекқорға ештеңе жазылмаған.
    """
    result = {"errors": [], "created": 0, "updated": 0, "rows": []}
    errors = result["errors"]

    try:
        workbook = load_workbook(file, read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, ValueError, OSError):
        errors.append(_("Файл оқылмады: ол Excel (.xlsx) файлы болуы керек."))
        return result
    try:
        rows = read_rows(workbook.worksheets[0], WIDTH)
    except TooManyRows:
        errors.append(
            _("Файлда жол тым көп: бір парақта ең көбі %(count)s жол.")
            % {"count": IMPORT_MAX_ROWS}
        )
        return result
    finally:
        workbook.close()

    headers = column_headers()

    def add_error(row, column, message):
        errors.append(
            _("%(row)s-жол, «%(column)s»: %(message)s")
            % {"row": row, "column": headers[column], "message": message}
        )

    groups = {group.name.strip().lower(): group for group in StudyGroup.objects.all()}
    students = []
    rows_by_iin = {}
    for row, cells in rows:
        full_name = " ".join(cell_text(cells[NAME]).split())
        group_name = cell_text(cells[GROUP]).strip()
        group = groups.get(group_name.lower())
        iin = normalize_iin(cells[IIN])

        if not full_name:
            add_error(row, NAME, _("бос болмауы керек."))
        elif any(len(part) > 150 for part in split_name(full_name)):
            add_error(row, NAME, _("тым ұзын."))
        if group is None:
            add_error(
                row,
                GROUP,
                _("«%(group)s» тобы жоқ: алдымен оны admin-де құрыңыз.") % {"group": group_name},
            )
        if not iin_is_valid(iin):
            add_error(row, IIN, _("12 цифрдан тұрып, бақылау цифры дұрыс болуы керек."))
        elif iin in rows_by_iin:
            add_error(row, IIN, _("бұл ИИН %(row)s-жолда бар.") % {"row": rows_by_iin[iin]})
        else:
            rows_by_iin[iin] = row
        students.append({"row": row, "name": full_name, "group": group, "iin": iin})

    existing = {
        user.username: user
        for user in User.objects.filter(username__in=rows_by_iin).select_related("profile")
    }
    for iin, user in existing.items():
        if user.is_staff:
            add_error(rows_by_iin[iin], IIN, _("бұл — қызметкердің логині, оны өзгертуге болмайды."))

    if not errors and not students:
        errors.append(_("Файлда студент жоқ."))
    if errors:
        return result

    save_students(students, existing, result)
    return result


def save_student(user, full_name, group):
    """Студенттің аты-жөні мен тобын сақтайды (профильді post_save сигналы жасайды)."""
    user.last_name, user.first_name = split_name(full_name)
    user.save()
    user.profile.group = group
    user.profile.save()


def students():
    """Студенттер: қызметкерлер (is_staff) мен әкімшілерсіз — «Студенттер» беттері тек осыларды көреді."""
    return User.objects.filter(is_staff=False, is_superuser=False)


def student_name(user):
    """Студенттің аты-жөні импорттағыдай: «Тегі Аты» (бос болса — логині)."""
    return f"{user.last_name} {user.first_name}".strip() or user.username


def delete_student(user):
    """Студентті өшіреді: профилі мен тест әрекеттері (нәтижелері) де бірге өшеді."""
    user.delete()


def update_student(user, name, group, iin):
    """Студентті өңдеу: аты-жөні, тобы, ИИН (логин). Коды өзгермейді."""
    user.username = iin
    save_student(user, name, group)


def create_student(full_name, group, iin):
    """
    Жаңа студент (импортта да, admin-дегі «Студент қосу» бетінде де): логин — ИИН,
    құпия сөз — жаңа код. Қайтарады: (студент, кодтың өзі).
    """
    user = User(username=iin)
    code = set_access_code(user)
    save_student(user, full_name, group)
    return user, code


@transaction.atomic
def save_students(students, existing, result):
    """Тексерілген студенттерді бір транзакцияда сақтайды."""
    for student in students:
        user = existing.get(student["iin"])
        code = None
        if user is None:
            user, code = create_student(student["name"], student["group"], student["iin"])
            result["created"] += 1
        else:
            save_student(user, student["name"], student["group"])
            result["updated"] += 1
        result["rows"].append(
            {"name": student["name"], "group": student["group"].name, "iin": student["iin"], "code": code}
        )


def result_message(result):
    """Сәтті импорттан кейінгі хабарлама."""
    return _(
        "Студенттер импортталды: жаңасы — %(created)s, жаңартылғаны — %(updated)s. "
        "Жаңа кодтар жүктелген файлда."
    ) % {"created": result["created"], "updated": result["updated"]}


# ---------- Кодтар ----------


@transaction.atomic
def regenerate_codes(users):
    """
    Студенттерге жаңа код жасайды (ескі код жұмыс істемейді). Қызметкерлер
    (is_staff) өзгермейді. Қайтарады: кодтар файлының жолдары.
    """
    rows = []
    for user in users.filter(is_staff=False).select_related("profile__group"):
        code = set_access_code(user)
        user.save(update_fields=["password"])
        group = user.profile.group if hasattr(user, "profile") else None
        rows.append(
            {
                "name": student_name(user),
                "group": group.name if group else "",
                "iin": user.username,
                "code": code,
            }
        )
    return rows


def codes_workbook(rows):
    """Кодтар файлы (.xlsx, bytes): Аты-жөні, Топ, ИИН, Код. ИИН мен код — мәтін (бас нөлдер сақталады)."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _("Кодтар")
    sheet.append(column_headers() + [_("Код")])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in sorted(rows, key=lambda item: (item["group"], item["name"])):
        code = row["code"] or _("бұрынғы код")
        sheet.append([row["name"], row["group"], row["iin"], code])
    for column in ("C", "D"):
        for cell in sheet[column]:
            cell.number_format = "@"
    for column, width in zip("ABCD", [40, 14, 16, 14]):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def import_template():
    """Бос үлгі файл: «Студенттер» парағы (тақырыптар мен мысал) және топтар тізімі."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _("Студенттер")
    sheet.append(column_headers())
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for cell in sheet["C"]:
        cell.number_format = "@"
    for column, width in zip("ABC", [40, 14, 16]):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"

    groups = workbook.create_sheet(_("Топтар"))
    groups.append([_("Топ"), _("Пән"), _("Оқыту тілі")])
    for cell in groups[1]:
        cell.font = Font(bold=True)
    for group in StudyGroup.objects.select_related("subject"):
        groups.append([group.name, group.subject.name, group.get_language_display()])
    groups.column_dimensions["A"].width = 14
    groups.column_dimensions["B"].width = 30
    groups.column_dimensions["C"].width = 14

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
