"""
«Студенттер» беттері (/students/...): тізім, қосу, өңдеу, жаңа код, Excel импорты.
Тек әкімші (is_superuser): сайтта тіркелу жоқ, студенттерді осы жерде қосады.
Кодтың өзі дерекқорда сақталмайды (хэші ғана), сондықтан ол тек бір рет —
қосқан не жаңа код жасаған сәтте экранда немесе импорттың кодтар файлында көрінеді.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.quiz.constants import IMPORT_MAX_MB, IMPORT_MAX_ROWS
from apps.quiz.forms import ExcelImportForm
from apps.quiz.question_import import XLSX_CONTENT_TYPE

from .forms import StudentFilterForm, StudentForm
from .student_import import (
    CODE_LENGTH,
    codes_workbook,
    create_student,
    import_students,
    import_template,
    regenerate_codes,
    result_message,
    students,
    update_student,
)

# Студенттер тізімінің бір бетіндегі жол саны
STUDENTS_PER_PAGE = 50


def superuser_required(view):
    """Тек әкімші: кірмеген — кіру бетіне, оқытушы мен студент — 403."""

    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def xlsx_response(content, name):
    """Excel файлын жүктеп алуға береді: name-2026-10-12.xlsx."""
    response = HttpResponse(content, content_type=XLSX_CONTENT_TYPE)
    filename = f"{name}-{timezone.localdate():%Y-%m-%d}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def show_code(request, student, code, created):
    """Студенттің коды бір рет көрсетілетін бет (created — жаңа студент пе, жаңа код па)."""
    context = {"student": student, "code": code, "created": created}
    return render(request, "students/code.html", context)


@superuser_required
def student_list(request):
    filter_form = StudentFilterForm(request.GET)
    users = filter_form.filter(
        students().select_related("profile__group__subject").order_by("last_name", "first_name")
    )
    page = Paginator(users, STUDENTS_PER_PAGE).get_page(request.GET.get("page"))

    # Бет ауыстырғанда сүзгі сақталуы үшін
    query = request.GET.copy()
    query.pop("page", None)
    context = {"filter_form": filter_form, "page": page, "query": query.urlencode()}
    return render(request, "students/list.html", context)


@never_cache
@superuser_required
def student_add(request):
    """Бір студентті қосу: сәтті болса, оның коды экранда бір рет көрсетіледі."""
    form = StudentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        student, code = create_student(data["full_name"], data["group"], data["iin"])
        return show_code(request, student, code, created=True)
    return render(request, "students/form.html", {"form": form, "student": None})


@superuser_required
def student_edit(request, pk):
    """Студенттің аты-жөнін, тобын, ИИН-ін өзгерту (коды өзгермейді)."""
    student = get_object_or_404(students(), pk=pk)
    form = StudentForm(request.POST or None, user=student)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        update_student(student, data["full_name"], data["group"], data["iin"])
        messages.success(request, _("Студенттің деректері сақталды."))
        return redirect("students:list")
    return render(request, "students/form.html", {"form": form, "student": student})


@never_cache
@superuser_required
@require_POST
def student_new_code(request, pk):
    """Студентке жаңа код (ескісі жұмыс істемейді): экранда бір рет көрсетіледі."""
    student = get_object_or_404(students(), pk=pk)
    code = regenerate_codes(students().filter(pk=student.pk))[0]["code"]
    return show_code(request, student, code, created=False)


@superuser_required
def student_import(request):
    """
    Excel-ден импорттау: GET — нұсқаулық пен форма, POST — файл тексеріліп,
    сәтті болса кодтар файлы бірден жүктеледі. Қате болса — ештеңе сақталмайды,
    қателер тізімі осы бетте көрсетіледі.
    """
    form = ExcelImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        result = import_students(form.cleaned_data["file"])
        if not result["errors"]:
            # Хабарлама келесі ашылған бетте (мысалы, тізімде) көрінеді
            messages.success(request, result_message(result))
            return xlsx_response(codes_workbook(result["rows"]), "student-codes")

    context = {
        "form": form,
        "result": result,
        "code_length": CODE_LENGTH,
        "max_rows": IMPORT_MAX_ROWS,
        "max_file_mb": IMPORT_MAX_MB,
    }
    return render(request, "students/import.html", context)


@superuser_required
def student_import_template(request):
    """Студенттер импортының бос үлгі файлы (топтар тізімімен)."""
    return xlsx_response(import_template(), "students-template")
