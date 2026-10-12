from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.quiz.forms import ExcelImportForm
from apps.quiz.question_import import XLSX_CONTENT_TYPE
from apps.quiz.services import active_attempt, first_unanswered_number

from .forms import StudentAddForm
from .models import StudyGroup
from .student_import import (
    codes_workbook,
    create_student,
    import_students,
    import_template,
    result_message,
)


@login_required
def after_login(request):
    """Кіргеннен кейін: оқытушы — сұрақтар бетіне, студент — кабинетке."""
    if request.user.is_staff:
        return redirect("quiz:teacher_questions")
    return redirect("quiz:dashboard")


@require_POST
def logout_view(request):
    """
    Шығу (тек POST). Тест жүріп жатса, аккаунттан шығуға болмайды: студент
    алдымен тестті аяқтауы керек (мерзімі өткен тест есептелмейді).
    """
    attempt = active_attempt(request.user)
    if attempt:
        messages.error(
            request, _("Тест аяқталмай тұрып аккаунттан шығуға болмайды. Алдымен тестті аяқтаңыз.")
        )
        return redirect("quiz:attempt_question", attempt.pk, first_unanswered_number(attempt))
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


# ---------- Студенттерді импорттау (admin, тек әкімші) ----------


def xlsx_response(content, name):
    """Excel файлын жүктеп алуға береді: name-2026-10-12.xlsx."""
    response = HttpResponse(content, content_type=XLSX_CONTENT_TYPE)
    filename = f"{name}-{timezone.localdate():%Y-%m-%d}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def require_superuser(request):
    """Студенттерді тек әкімші (is_superuser) импорттайды; оқытушыға — 403."""
    if not request.user.is_superuser:
        raise PermissionDenied


def import_students_view(request):
    """
    Admin-дегі «Студенттерді импорттау» беті: GET — нұсқаулық пен форма,
    POST — файл тексеріліп, сәтті болса кодтар файлы бірден жүктеледі.
    Қате болса — ештеңе сақталмайды, қателер тізімі осы бетте көрсетіледі.
    """
    require_superuser(request)
    form = ExcelImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        result = import_students(form.cleaned_data["file"])
        if not result["errors"]:
            # Хабарлама admin-нің келесі ашылған бетінде көрінеді
            messages.success(request, result_message(result))
            return xlsx_response(codes_workbook(result["rows"]), "student-codes")

    context = {
        **admin.site.each_context(request),
        "title": _("Студенттерді импорттау"),
        "opts": StudyGroup._meta,
        "form": form,
        "result": result,
    }
    return render(request, "admin/accounts/import_students.html", context)


@never_cache
def add_student_view(request):
    """
    Admin-дегі «Студент қосу» беті (тек әкімші): бір студентті қолмен қосу.
    Сәтті болса, сол бетте студенттің коды бір рет көрсетіледі (дерекқорда хэші
    ғана, сондықтан кейін көрсету мүмкін емес) — бет кэштелмейді.
    """
    require_superuser(request)
    form = StudentAddForm(request.POST or None)
    created = None
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        user, code = create_student(data["full_name"], data["group"], data["iin"])
        created = {"user": user, "name": data["full_name"], "group": data["group"], "code": code}
        form = StudentAddForm()

    context = {
        **admin.site.each_context(request),
        "title": _("Студент қосу"),
        "opts": StudyGroup._meta,
        "form": form,
        "created": created,
    }
    return render(request, "admin/accounts/add_student.html", context)


def import_students_template_view(request):
    """Студенттер импортының бос үлгі файлы (топтар тізімімен)."""
    require_superuser(request)
    return xlsx_response(import_template(), "students-template")
