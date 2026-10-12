"""
Оқытушы беттері (/teacher/...): сұрақтар, контексттер, банк толуы, нәтижелер.
Барлығы тек оқытушыға (is_staff) ашық және тек оқытушының таңдалған пәні
бойынша жұмыс істейді (TZ.md, 10.4): басқа пәннің сұрағы, контексті — 404.
"""

from functools import wraps
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .constants import (
    IMPORT_MAX_MB,
    IMPORT_MAX_ROWS,
    MAX_IMAGE_MB,
    QUESTIONS_PER_CONTEXT,
    QUESTIONS_TOTAL,
)
from .forms import (
    AnswerFormSet,
    ContextForm,
    QuestionFilterForm,
    QuestionForm,
    ExcelImportForm,
    ResultFilterForm,
    SampleVariantForm,
    SubjectSelectForm,
    initial_from_query,
)
from .models import Attempt, Context, Language, Question
from .question_import import (
    CONTEXTS_SHEET,
    QUESTIONS_SHEET,
    XLSX_CONTENT_TYPE,
    import_questions,
    import_template,
    result_message,
)
from .services import (
    AttemptError,
    bank_coverage,
    build_variant,
    choose_subject,
    copy_question,
    finish_expired_attempts,
    results_rows,
    results_summary,
    save_question,
    teacher_subjects,
    toggle_question_active,
    variant_summary,
    write_results_csv,
)

# Сұрақтар тізімінің бір бетіндегі жол саны
QUESTIONS_PER_PAGE = 50

# Оқытушының таңдалған пәні Django сессиясында осы кілтпен сақталады
SUBJECT_SESSION_KEY = "teacher_subject_id"

# Пән ауыстырылғанда өңдеу бетінен (онда бұрынғы пәннің сұрағы) тізімге қайтамыз
LIST_PAGES = {
    "teacher_question_edit": "quiz:teacher_questions",
    "teacher_context_edit": "quiz:teacher_contexts",
}


def subject_required(view):
    """
    Оқытушы беті таңдалған пәнмен шақырылады: view(request, subject, ...).
    Пәндер тізімі мен таңдалған пән шаблонға (пән ауыстырғышы) request арқылы
    беріледі. Пәні тағайындалмаған оқытушыға хабарлама беті шығады.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        subjects = list(teacher_subjects(request.user))
        subject = choose_subject(subjects, request.session.get(SUBJECT_SESSION_KEY))
        if subject is None:
            return render(request, "teacher/no_subject.html", status=403)
        request.teacher_subjects = subjects
        request.subject = subject
        return view(request, subject, *args, **kwargs)

    return wrapper


def redirect_back(request, default):
    """Формадағы `next` мекенжайына (тек осы сайт ішінде) немесе default-қа қайтарады."""
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect(default)


# ---------- Пән ауыстырғышы ----------


@staff_member_required
@require_POST
def subject_select(request):
    """Оқытушының пәнін ауыстырады да, сол бөлімнің бетіне қайтарады (сүзгісіз)."""
    form = SubjectSelectForm(request.POST, subjects=teacher_subjects(request.user))
    if form.is_valid():
        request.session[SUBJECT_SESSION_KEY] = form.cleaned_data["subject"].pk

    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect("quiz:teacher_questions")
    # Сүзгі параметрлері (тақырып, сессия) бұрынғы пәнге жатады — алып тастаймыз
    path = urlsplit(next_url).path
    try:
        url_name = resolve(path).url_name
    except Resolver404:
        return redirect("quiz:teacher_questions")
    if url_name in LIST_PAGES:
        return redirect(LIST_PAGES[url_name])
    return redirect(path)


# ---------- Сұрақтар ----------


def subject_questions(subject):
    """Пәннің сұрақтары (оқытушы тек осыларды ашып, өзгерте алады)."""
    return Question.objects.filter(subtopic__topic__subject=subject)


@staff_member_required
@subject_required
def question_list(request, subject):
    filter_form = QuestionFilterForm(request.GET, subject=subject)
    questions = filter_form.filter(
        subject_questions(subject).select_related("subtopic__topic", "context")
    )
    page = Paginator(questions, QUESTIONS_PER_PAGE).get_page(request.GET.get("page"))

    # Бет ауыстырғанда сүзгі сақталуы үшін
    query = request.GET.copy()
    query.pop("page", None)

    return render(
        request,
        "teacher/question_list.html",
        {"filter_form": filter_form, "page": page, "query": query.urlencode()},
    )


def question_form_page(request, subject, question=None):
    """Сұрақ енгізу (question=None) және өңдеу беттерінің ортақ бөлігі."""
    if request.method == "POST":
        form = QuestionForm(request.POST, request.FILES, instance=question, subject=subject)
        formset = AnswerFormSet(request.POST, request.FILES, instance=form.instance)
        if form.is_valid() and formset.is_valid():
            question = save_question(form, formset)
            if "save_and_next" in request.POST:
                messages.success(request, _("Сұрақ сақталды. Келесісін енгізіңіз."))
                new_url = reverse("quiz:teacher_question_create")
                return redirect(f"{new_url}?{form.next_question_query()}")
            messages.success(request, _("Сұрақ сақталды."))
            return redirect("quiz:teacher_question_edit", pk=question.pk)
    else:
        initial = initial_from_query(request.GET) if question is None else None
        form = QuestionForm(instance=question, initial=initial, subject=subject)
        formset = AnswerFormSet(instance=form.instance)

    # Алдын ала қарау — сақталған нұсқасы (форма өзгерткен объект емес)
    preview = None
    if question is not None:
        preview = Question.objects.select_related("context").get(pk=question.pk)

    return render(
        request,
        "teacher/question_form.html",
        {
            "form": form,
            "formset": formset,
            "question": question,
            "preview": preview,
            # Формулалар: кеңес көрсетіледі, алдын ала қарауда KaTeX қосылады
            "uses_formulas": subject.uses_formulas,
        },
    )


@staff_member_required
@subject_required
def question_create(request, subject):
    return question_form_page(request, subject)


@staff_member_required
@subject_required
def question_edit(request, subject, pk):
    question = get_object_or_404(subject_questions(subject), pk=pk)
    return question_form_page(request, subject, question)


@staff_member_required
@subject_required
@require_POST
def question_copy(request, subject, pk):
    question = get_object_or_404(subject_questions(subject), pk=pk)
    copy = copy_question(question)
    messages.success(request, _("Көшірмесі жасалды, қажет болса өңдеңіз."))
    return redirect("quiz:teacher_question_edit", pk=copy.pk)


@staff_member_required
@subject_required
@require_POST
def question_toggle(request, subject, pk):
    question = get_object_or_404(subject_questions(subject), pk=pk)
    if not toggle_question_active(question):
        messages.error(
            request,
            _("Контекстте %(count)s белсенді сұрақ бар, бұл сұрақты белсендіруге болмайды.")
            % {"count": QUESTIONS_PER_CONTEXT},
        )
    return redirect_back(request, "quiz:teacher_questions")


# ---------- Сұрақтарды Excel-ден импорттау ----------


@staff_member_required
@subject_required
def question_import(request, subject):
    """
    Сұрақтарды Excel-ден импорттау: GET — нұсқаулық пен форма, POST — файлды жүктеу.
    Қате болса — ештеңе сақталмайды, қателер тізімі осы бетте көрсетіледі.
    """
    form = ExcelImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        result = import_questions(subject, form.cleaned_data["file"])
        if not result["errors"]:
            messages.success(request, result_message(result))
            return redirect("quiz:teacher_questions")

    context = {
        "form": form,
        "result": result,
        "subject": subject,
        "max_rows": IMPORT_MAX_ROWS,
        "max_image_mb": MAX_IMAGE_MB,
        "max_file_mb": IMPORT_MAX_MB,
        "questions_per_context": QUESTIONS_PER_CONTEXT,
        "questions_sheet": QUESTIONS_SHEET,
        "contexts_sheet": CONTEXTS_SHEET,
    }
    return render(request, "teacher/question_import.html", context)


@staff_member_required
@subject_required
def question_import_template(request, subject):
    """Таңдалған пәннің бос үлгі файлы (.xlsx)."""
    response = HttpResponse(import_template(subject), content_type=XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="questions-{subject.code}.xlsx"'
    return response


# ---------- Контексттер ----------


@staff_member_required
@subject_required
def context_list(request, subject):
    contexts = Context.objects.filter(subject=subject).annotate(
        active_count=Count("questions", filter=Q(questions__is_active=True))
    )
    return render(
        request,
        "teacher/context_list.html",
        {"contexts": contexts, "questions_per_context": QUESTIONS_PER_CONTEXT},
    )


def context_form_page(request, subject, quiz_context=None):
    """Контекст жасау (quiz_context=None) және өңдеу беттерінің ортақ бөлігі."""
    # Жаңа контекст оқытушының таңдалған пәніне жазылады
    instance = quiz_context or Context(subject=subject)
    if request.method == "POST":
        form = ContextForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            quiz_context = form.save()
            messages.success(request, _("Контекст сақталды."))
            return redirect("quiz:teacher_context_edit", pk=quiz_context.pk)
    else:
        form = ContextForm(instance=instance)

    questions = []
    active_count = 0
    if quiz_context is not None:
        questions = quiz_context.questions.select_related("subtopic")
        active_count = quiz_context.active_questions_count()

    return render(
        request,
        "teacher/context_form.html",
        {
            "form": form,
            "quiz_context": quiz_context,
            "questions": questions,
            "active_count": active_count,
            "questions_per_context": QUESTIONS_PER_CONTEXT,
        },
    )


@staff_member_required
@subject_required
def context_create(request, subject):
    return context_form_page(request, subject)


@staff_member_required
@subject_required
def context_edit(request, subject, pk):
    quiz_context = get_object_or_404(Context, pk=pk, subject=subject)
    return context_form_page(request, subject, quiz_context)


# ---------- Банк толуы ----------


@staff_member_required
@subject_required
def bank(request, subject):
    return render(request, "teacher/bank.html", {"coverage": bank_coverage(subject)})


@staff_member_required
@subject_required
@never_cache
def bank_sample(request, subject):
    """
    Үлгі нұсқа: build_variant() банктен нұсқа құрады, бірақ дерекқорға ештеңе
    жазылмайды (Attempt жасалмайды). Бет жаңартылған сайын жаңа нұсқа шығады.
    """
    language = SampleVariantForm(request.GET).get_language()
    context = {
        "language": language,
        "languages": Language.choices,
        "uses_formulas": subject.uses_formulas,
    }
    try:
        context["summary"] = variant_summary(subject, build_variant(subject, language))
    except AttemptError as error:
        context["error"] = str(error)
    return render(request, "teacher/bank_sample.html", context)


# ---------- Нәтижелер ----------

# Нәтижелер кестесінің бір бетіндегі жол саны
RESULTS_PER_PAGE = 50


def filtered_attempts(request, subject):
    """
    Пәннің сүзгіден өткен әрекеттері (нәтижелер беті мен CSV үшін ортақ).
    Алдымен мерзімі өткен әрекеттер аяқталады — балы дұрыс көрінсін.
    """
    finish_expired_attempts()
    filter_form = ResultFilterForm(request.GET, subject=subject)
    attempts = filter_form.filter(
        Attempt.objects.filter(session__subject=subject)
        .select_related("user__profile__group", "session__subject")
        .order_by(
            "-session__opens_at", "user__last_name", "user__first_name", "user__username"
        )
    )
    return filter_form, attempts


@staff_member_required
@subject_required
def results(request, subject):
    filter_form, attempts = filtered_attempts(request, subject)
    page = Paginator(attempts, RESULTS_PER_PAGE).get_page(request.GET.get("page"))

    # Бет ауыстырғанда және CSV жүктегенде сүзгі сақталуы үшін
    query = request.GET.copy()
    query.pop("page", None)

    context = {
        "filter_form": filter_form,
        "page": page,
        "rows": results_rows(page),
        "summary": results_summary(list(attempts)),
        "query": query.urlencode(),
        "questions_total": QUESTIONS_TOTAL,
    }
    return render(request, "teacher/results.html", context)


@staff_member_required
@subject_required
def results_export(request, subject):
    """Сүзгіден өткен нәтижелерді CSV файл етіп береді."""
    _filter_form, attempts = filtered_attempts(request, subject)
    filename = f"results-{subject.code}-{timezone.localdate():%Y-%m-%d}.csv"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    write_results_csv(attempts, response)
    return response
