from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .constants import ANSWERS_PER_QUESTION
from .forms import AnswerInlineFormSet, ExamSessionAdminForm
from .models import (
    Answer,
    Attempt,
    AttemptQuestion,
    Context,
    ExamSession,
    Question,
    Subject,
    Subtopic,
    Topic,
)


# Пәндер мен тақырыптар `load_subjects` арқылы жүктеледі (subjects.json).
# Пәнді admin-де тек белсенді/белсенді емес етуге болады, тақырыптар тек оқылады.


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ["order", "code", "name_kk", "name_ru", "duration_minutes", "is_active"]
    list_display_links = ["code"]
    readonly_fields = ["code", "name_kk", "name_ru", "duration_minutes", "uses_formulas", "order"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SubtopicInline(admin.TabularInline):
    model = Subtopic
    extra = 0
    can_delete = False
    fields = ["number", "name_kk", "name_ru", "description_kk"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ["number", "name_kk", "name_ru", "subject"]
    list_filter = ["subject"]
    readonly_fields = ["subject", "number", "name_kk", "name_ru"]
    inlines = [SubtopicInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Subtopic)
class SubtopicAdmin(admin.ModelAdmin):
    list_display = ["number", "topic", "name_kk", "name_ru"]
    list_filter = ["topic__subject", "topic"]
    readonly_fields = ["topic", "number", "name_kk", "name_ru", "description_kk"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# Сұрақтар негізінен /teacher/... беттерінде енгізіледі; admin — қосалқы құрал


class AnswerInline(admin.TabularInline):
    model = Answer
    formset = AnswerInlineFormSet
    max_num = ANSWERS_PER_QUESTION

    def get_extra(self, request, obj=None, **kwargs):
        # Жаңа сұраққа 4 бос жол, бар сұрақта — жаңа жол жоқ
        if obj is None:
            return ANSWERS_PER_QUESTION
        return 0


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ["id", "subtopic", "level", "language", "short_text", "is_demo", "is_active"]
    list_filter = [
        "subtopic__topic__subject",
        "language",
        "level",
        "is_active",
        "is_demo",
        "subtopic__topic",
        "subtopic",
    ]
    search_fields = ["text"]
    list_select_related = ["subtopic"]
    autocomplete_fields = ["context"]
    inlines = [AnswerInline]

    @admin.display(description=_("мәтіні"))
    def short_text(self, obj):
        return obj.text[:80]


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 0
    fields = ["subtopic", "level", "text", "is_active"]
    show_change_link = True


@admin.register(Context)
class ContextAdmin(admin.ModelAdmin):
    list_display = ["id", "title", "subject", "language", "question_count", "is_demo", "is_active"]
    list_filter = ["subject", "language", "is_active", "is_demo"]
    search_fields = ["title", "text"]
    inlines = [QuestionInline]

    @admin.display(description=_("сұрақтар"))
    def question_count(self, obj):
        return obj.questions.count()


# ---------- Сессиялар мен нәтижелер ----------


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    """Admin-дегі сессиялар — тек әкімшіге; оқытушы сессияларды /teacher/sessions/ бетінде құрады."""

    form = ExamSessionAdminForm
    list_display = ["title", "subject", "opens_at", "closes_at", "show_answers", "is_active"]
    list_filter = ["subject", "is_active", "groups"]
    search_fields = ["title"]
    filter_horizontal = ["groups"]


class AttemptQuestionInline(admin.TabularInline):
    model = AttemptQuestion
    extra = 0
    can_delete = False
    fields = ["order", "question", "selected"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    """Тест әрекеттері admin-де тек оқылады — нәтижені қолмен өзгертуге болмайды."""

    list_display = ["user", "session", "language", "status", "score", "started_at"]
    list_filter = ["status", "language", "session"]
    search_fields = ["user__username", "user__last_name"]
    inlines = [AttemptQuestionInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
