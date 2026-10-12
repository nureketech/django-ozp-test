from urllib.parse import urlencode

from django import forms
from django.db.models import Q
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import StudyGroup

from .constants import ANSWERS_PER_QUESTION, IMPORT_MAX_MB, QUESTIONS_PER_CONTEXT
from .models import (
    LEVEL_FULL_DESCRIPTIONS,
    LEVEL_SHORT_DESCRIPTIONS,
    Answer,
    Context,
    ExamSession,
    Language,
    Level,
    Question,
    Subject,
    Subtopic,
    Topic,
)


ANSWER_EMPTY_ERROR = _("Әр жауап нұсқасында мәтін немесе сурет болуы керек.")
USED_QUESTION_ERROR = _(
    "Бұл сұрақ студенттердің тестінде қолданылған, дұрыс жауабын өзгертуге болмайды. "
    "Көшірмесін жасап, соны өңдеңіз."
)

# Сурет өрістері: браузер тек рұқсат етілген пішімдерді ұсынады (тексеруді сервер жасайды)
IMAGE_ACCEPT = "image/jpeg,image/png,image/webp"


def answer_has_content(cleaned_data):
    """Жауап нұсқасында мәтін немесе сурет бар ма (сурет өшірілсе — False)."""
    return bool(cleaned_data.get("text", "").strip() or cleaned_data.get("image"))


class AnswerInlineFormSet(forms.BaseInlineFormSet):
    """
    Сұрақтың жауап нұсқаларын тексереді (admin үшін): дәл 4 нұсқа,
    олардың дәл біреуі дұрыс, әрқайсында мәтін не сурет бар.
    """

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        filled_count = 0
        correct_count = 0
        for form in self.forms:
            # Жойылатын немесе бос қалған жолдарды санамаймыз
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            filled_count += 1
            if form.cleaned_data.get("is_correct"):
                correct_count += 1
            if not answer_has_content(form.cleaned_data):
                raise forms.ValidationError(ANSWER_EMPTY_ERROR)

        if filled_count != ANSWERS_PER_QUESTION:
            raise forms.ValidationError(
                _("Дәл %(count)s жауап нұсқасы болуы керек.")
                % {"count": ANSWERS_PER_QUESTION}
            )
        if correct_count != 1:
            raise forms.ValidationError(_("Дәл бір дұрыс жауап белгіленуі керек."))


# ---------- Ортақ өрістер мен виджеттер ----------


def add_bootstrap_classes(form):
    """Өрістерге Bootstrap 5 класстарын қосады (шаблон қарапайым болуы үшін)."""
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, (forms.RadioSelect, forms.CheckboxInput)):
            css_class = "form-check-input"
        elif isinstance(widget, forms.Select):
            css_class = "form-select"
        else:
            css_class = "form-control"
        widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css_class}".strip()


class CodeField(forms.CharField):
    """
    Код жазылатын өріс: шегіністер мен бос орындар өзгертілмейді.
    Тек браузер жіберетін жол соңы (\\r\\n) қарапайым \\n-ге ауыстырылады.
    """

    def __init__(self, rows=6, **kwargs):
        kwargs.setdefault("strip", False)
        kwargs.setdefault(
            "widget",
            forms.Textarea(
                attrs={"rows": rows, "class": "font-monospace", "spellcheck": "false"}
            ),
        )
        super().__init__(**kwargs)

    def to_python(self, value):
        value = super().to_python(value)
        value = value.replace("\r\n", "\n")
        # Тек бос орындардан тұрса — өріс бос деп саналады
        if not value.strip():
            return ""
        return value


class SubtopicSelect(forms.Select):
    """
    Тақырыпшалар тізімі: әр <option>-ға оның тақырыбы жазылады (data-topic).
    static/js/subtopic_filter.js сол бойынша тек таңдалған тақырыптың тақырыпшаларын қалдырады.
    Спецификациядағы толық мазмұны (description_kk) подсказка (title) болып шығады.
    """

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        # Бос жолдан ("---------") басқасының мәнінде тақырыпша объектісі бар
        if value:
            option["attrs"]["data-topic"] = value.instance.topic_id
            if value.instance.description_kk:
                option["attrs"]["title"] = value.instance.description_kk
        return option


class ContextSelect(forms.Select):
    """
    Контексттер тізімі: әр <option>-ға оның тілі жазылады (data-language).
    static/js/subtopic_filter.js сол бойынша тек сұрақ тіліндегі контексттерді қалдырады.
    """

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value:
            option["attrs"]["data-language"] = value.instance.language
        return option


class LevelRadioSelect(forms.RadioSelect):
    """Деңгей радио-батырмалары: толық сипаттамасы подсказка (title) болып шығады."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        option["attrs"]["title"] = LEVEL_FULL_DESCRIPTIONS.get(value, "")
        return option


# ---------- Сұрақ енгізу формасы ----------

# «Сақтап, келесісін қосу» және контекст бетіндегі «Сұрақ қосу» жіберетін URL параметрлері
STICKY_FIELDS = ["topic", "subtopic", "level", "language", "context"]


def initial_from_query(query):
    """URL параметрлерінен (?topic=2&subtopic=3...) жаңа сұрақ формасының бастапқы мәндері."""
    return {name: query[name] for name in STICKY_FIELDS if query.get(name)}


class QuestionForm(forms.ModelForm):
    """
    Оқытушының сұрақ енгізу формасы (subject — оқытушының таңдалған пәні:
    тақырыптар, тақырыпшалар мен контексттер тек сол пәннен).
    Өрістер реті: тақырып → тақырыпша → деңгей → тіл → контекст → мәтін, код, сурет.
    Жауап нұсқалары бөлек — AnswerFormSet.
    """

    # Тақырып сұраққа сақталмайды: ол тек тақырыпшаларды сүзу үшін керек
    topic = forms.ModelChoiceField(label=_("Тақырып"), queryset=Topic.objects.none())
    subtopic = forms.ModelChoiceField(
        label=_("Тақырыпша"),
        queryset=Subtopic.objects.none(),
        widget=SubtopicSelect,
    )
    level = forms.ChoiceField(
        label=_("Деңгей"),
        choices=[(value, LEVEL_SHORT_DESCRIPTIONS[value]) for value in Level.values],
        widget=LevelRadioSelect,
    )
    language = forms.ChoiceField(
        label=_("Тіл"), choices=Language.choices, widget=forms.RadioSelect
    )
    context = forms.ModelChoiceField(
        label=_("Контекст"),
        queryset=Context.objects.none(),
        required=False,
        empty_label=_("Жоқ (жеке сұрақ)"),
        widget=ContextSelect,
    )
    text = forms.CharField(label=_("Сұрақ мәтіні"), widget=forms.Textarea(attrs={"rows": 4}))
    code = CodeField(label=_("Код"), required=False)

    field_order = ["topic", "subtopic", "level", "language", "context", "text", "code", "image"]

    class Meta:
        model = Question
        fields = ["subtopic", "level", "language", "context", "text", "code", "image"]
        labels = {"image": _("Сурет")}

    def __init__(self, *args, subject, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["topic"].queryset = Topic.objects.filter(subject=subject)
        self.fields["subtopic"].queryset = Subtopic.objects.filter(
            topic__subject=subject
        ).select_related("topic")
        # Тізімде пәннің белсенді контексттері (өңделіп жатқан сұрақтың контексті —
        # белсенді болмаса да)
        self.fields["context"].queryset = Context.objects.filter(subject=subject).filter(
            Q(is_active=True) | Q(pk=self.instance.context_id)
        )
        if self.instance.pk:
            self.initial["topic"] = self.instance.subtopic.topic_id
        self.fields["image"].widget.attrs["accept"] = IMAGE_ACCEPT
        add_bootstrap_classes(self)

    def clean(self):
        cleaned_data = super().clean()

        # Тақырыпша таңдалған тақырыпқа жатуы керек (JS сүзгісіне сенбейміз)
        topic = cleaned_data.get("topic")
        subtopic = cleaned_data.get("subtopic")
        if topic and subtopic and subtopic.topic_id != topic.pk:
            self.add_error("subtopic", _("Бұл тақырыпша таңдалған тақырыпқа жатпайды."))

        # Контекстте 5-тен көп белсенді сұрақ болмайды
        context = cleaned_data.get("context")
        if context and self.instance.is_active:
            others = context.questions.filter(is_active=True).exclude(pk=self.instance.pk)
            if others.count() >= QUESTIONS_PER_CONTEXT:
                self.add_error(
                    "context",
                    _("Бұл контекстте %(count)s сұрақ бар, жаңасын қосуға болмайды.")
                    % {"count": QUESTIONS_PER_CONTEXT},
                )
        # Контекст пен сұрақ тілінің сәйкестігін модельдің clean() әдісі тексереді
        return cleaned_data

    def next_question_query(self):
        """
        «Сақтап, келесісін қосу»: тақырып, тақырыпша, деңгей мен тіл келесі сұраққа
        сақталады (контекст те — онда әлі орын болса). Сақталғаннан кейін шақырылады.
        """
        question = self.instance
        params = {
            "topic": question.subtopic.topic_id,
            "subtopic": question.subtopic_id,
            "level": question.level,
            "language": question.language,
        }
        if (
            question.context_id
            and question.context.active_questions_count() < QUESTIONS_PER_CONTEXT
        ):
            params["context"] = question.context_id
        return urlencode(params)


class AnswerForm(forms.ModelForm):
    """
    Бір жауап нұсқасы: мәтін (код болуы мүмкін, шегіністер сақталады)
    немесе сурет, не екеуі де. Екеуі де бос болса — қате.
    """

    text = CodeField(label=_("Жауап нұсқасы"), rows=2, required=False)

    class Meta:
        model = Answer
        fields = ["text", "image"]
        labels = {"image": _("Сурет")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["image"].widget.attrs["accept"] = IMAGE_ACCEPT
        add_bootstrap_classes(self)

    def clean(self):
        cleaned_data = super().clean()
        if not self.errors and not answer_has_content(cleaned_data):
            raise forms.ValidationError(ANSWER_EMPTY_ERROR)
        return cleaned_data


class QuestionAnswersFormSet(forms.BaseInlineFormSet):
    """
    Сұрақ формасындағы 4 жауап нұсқасы. Дұрыс жауап бір радио-батырмамен
    белгіленеді: атауы `answers-correct`, мәні — нұсқаның реті (0–3).
    """

    @property
    def correct_name(self):
        return f"{self.prefix}-correct"

    @cached_property
    def used_in_attempts(self):
        """Сұрақ студенттердің тестінде қолданылды ма (онда дұрыс жауабы өзгермейді)."""
        return self.instance.pk is not None and self.instance.attempt_items.exists()

    def saved_correct_value(self):
        """Дерекқорда қай нұсқа дұрыс ("0"–"3", жаңа сұрақта "")."""
        for index, form in enumerate(self.forms):
            if form.instance.pk and form.instance.is_correct:
                return str(index)
        return ""

    def correct_value(self):
        """Қай нұсқа дұрыс деп белгіленген ("0"–"3", белгісіз болса "") — шаблон үшін."""
        if self.is_bound:
            return self.data.get(self.correct_name, "")
        return self.saved_correct_value()

    def clean(self):
        super().clean()
        allowed_values = [str(index) for index in range(len(self.forms))]
        value = self.data.get(self.correct_name)
        if value not in allowed_values:
            raise forms.ValidationError(_("Дұрыс жауапты белгілеңіз."))
        # Тестте қолданылған сұрақтың дұрыс жауабы ауысса, ескі нәтижелердің
        # балы бұзылады (сақталған балл мен нәтиже беті сәйкес келмей қалады)
        if self.used_in_attempts and value != self.saved_correct_value():
            raise forms.ValidationError(USED_QUESTION_ERROR)
        self.correct_index = int(value)

    def save(self, commit=True):
        # Мәтіні өзгермеген нұсқаның да «дұрыс» белгісі өзгеруі мүмкін — бәрін сақтаймыз
        answers = []
        for index, form in enumerate(self.forms):
            answer = form.save(commit=False)
            answer.question = self.instance
            answer.is_correct = index == self.correct_index
            answer.save()
            answers.append(answer)
        return answers


# Әр сұрақта дәл 4 нұсқа: бәрі міндетті, артық жол қосылмайды
AnswerFormSet = forms.inlineformset_factory(
    Question,
    Answer,
    form=AnswerForm,
    formset=QuestionAnswersFormSet,
    extra=0,
    min_num=ANSWERS_PER_QUESTION,
    max_num=ANSWERS_PER_QUESTION,
    validate_min=True,
    validate_max=True,
    can_delete=False,
)


# ---------- Сұрақтарды Excel-ден импорттау ----------


class ExcelImportForm(forms.Form):
    """Excel файлын жүктеу (сұрақтар мен студенттер импорты): тек .xlsx, IMPORT_MAX_MB-тан аспайды."""

    file = forms.FileField(label=_("Excel файлы (.xlsx)"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].widget.attrs["accept"] = ".xlsx"
        add_bootstrap_classes(self)

    def clean_file(self):
        file = self.cleaned_data["file"]
        if not file.name.lower().endswith(".xlsx"):
            raise forms.ValidationError(_("Файл Excel (.xlsx) пішімінде болуы керек."))
        if file.size > IMPORT_MAX_MB * 1024 * 1024:
            raise forms.ValidationError(
                _("Файл тым үлкен: ең көбі %(size)s МБ.") % {"size": IMPORT_MAX_MB}
            )
        return file


# ---------- Контекст формасы ----------


class ContextForm(forms.ModelForm):
    """Контекст (мәтін, кесте, график, сурет) — оған кейін 5 сұрақ байланады."""

    language = forms.ChoiceField(
        label=_("Тіл"), choices=Language.choices, widget=forms.RadioSelect
    )
    text = forms.CharField(label=_("Мәтіні"), widget=forms.Textarea(attrs={"rows": 8}))
    code = CodeField(label=_("Код"), required=False)

    class Meta:
        model = Context
        fields = ["title", "language", "text", "code", "image", "is_active"]
        labels = {
            "title": _("Атауы"),
            "image": _("Сурет"),
            "is_active": _("Белсенді"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["image"].widget.attrs["accept"] = IMAGE_ACCEPT
        add_bootstrap_classes(self)

    def clean_language(self):
        language = self.cleaned_data["language"]
        # Сұрақтары бар контексттің тілін өзгертсе, сұрақтармен тілі сәйкес келмей қалады
        if self.instance.pk and self.instance.questions.exclude(language=language).exists():
            raise forms.ValidationError(
                _("Бұл контекстке басқа тілдегі сұрақтар байланған, тілін өзгертуге болмайды.")
            )
        return language


# ---------- Оқытушының пәні ----------


class SubjectSelectForm(forms.Form):
    """Пән ауыстырғышы: оқытушы тек өзіне тағайындалған пәнді таңдай алады."""

    subject = forms.ModelChoiceField(queryset=Subject.objects.none())

    def __init__(self, *args, subjects, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subject"].queryset = subjects


# ---------- Сессия (admin) ----------


class ExamSessionAdminForm(forms.ModelForm):
    """Admin-дегі сессия формасы: таңдалған топтардың бәрі сессияның пәнінен."""

    class Meta:
        model = ExamSession
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        subject = cleaned_data.get("subject")
        groups = cleaned_data.get("groups")
        if subject and groups:
            foreign = [group.name for group in groups if group.subject_id != subject.pk]
            if foreign:
                self.add_error(
                    "groups",
                    _("Бұл топтар басқа пәнге жатады: %(groups)s")
                    % {"groups": ", ".join(foreign)},
                )
        return cleaned_data


# ---------- Сессия (оқытушының беті) ----------


class DateTimeLocalInput(forms.DateTimeInput):
    """Браузердің күн мен уақыт таңдағышы (<input type="datetime-local">), Asia/Almaty уақыты."""

    input_type = "datetime-local"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format="%Y-%m-%dT%H:%M")


class ExamSessionForm(forms.ModelForm):
    """
    Оқытушының сессия формасы (/teacher/sessions/): пән — оқытушының таңдалған пәні
    (формада өріс жоқ), топтар тізімінде тек сол пәннің топтары. Жабылу уақыты
    ашылудан кейін болуын модельдің clean() әдісі тексереді.
    """

    class Meta:
        model = ExamSession
        fields = ["title", "opens_at", "closes_at", "groups", "show_answers", "is_active"]
        labels = {
            "title": _("Атауы"),
            "opens_at": _("Ашылу уақыты"),
            "closes_at": _("Жабылу уақыты"),
            "groups": _("Топтар"),
            "show_answers": _("Дұрыс жауаптарды көрсету"),
            "is_active": _("Белсенді"),
        }
        widgets = {
            "opens_at": DateTimeLocalInput(),
            "closes_at": DateTimeLocalInput(),
            "groups": forms.CheckboxSelectMultiple,
            "show_answers": forms.RadioSelect,
        }

    def __init__(self, *args, subject, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = StudyGroup.objects.filter(subject=subject)
        self.fields["groups"].help_text = _(
            "Ешқайсысы белгіленбесе — сессия пәннің барлық тобына арналады."
        )
        add_bootstrap_classes(self)


# ---------- Тест тапсыру (студент) ----------


class AttemptAnswerForm(forms.Form):
    """
    Сұраққа жауап (тест пен жаттығуда): таңдалған нұсқа тек осы сұрақтың
    нұсқаларының бірі бола алады (бөтен сұрақтың жауабын жіберуге болмайды).
    answer_ids — сұрақтың жауап нұсқаларының id тізімі.
    """

    answer = forms.TypedChoiceField(coerce=int)

    def __init__(self, answer_ids, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["answer"].choices = [(answer_id, answer_id) for answer_id in answer_ids]


class PracticeStartForm(forms.Form):
    """Тақырыптық жаттығуды бастау: студенттің пәнінің тақырыбы және сұрақтар тілі."""

    topic = forms.ModelChoiceField(
        label=_("Тақырып"), queryset=Topic.objects.none(), empty_label=None
    )
    language = forms.ChoiceField(
        label=_("Сұрақтар тілі"), choices=Language.choices, widget=forms.RadioSelect
    )

    def __init__(self, *args, subject, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["topic"].queryset = Topic.objects.filter(subject=subject)
        add_bootstrap_classes(self)


# ---------- Оқытушының нәтижелер беті ----------


class ResultFilterForm(forms.Form):
    """Нәтижелер сүзгісі (GET): сессия және топ — тек оқытушының таңдалған пәнінен."""

    session = forms.ModelChoiceField(
        label=_("Сессия"),
        queryset=ExamSession.objects.none(),
        required=False,
        empty_label=_("Барлық сессия"),
    )
    group = forms.ModelChoiceField(
        label=_("Топ"),
        queryset=StudyGroup.objects.none(),
        required=False,
        empty_label=_("Барлық топ"),
    )

    def __init__(self, *args, subject, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["session"].queryset = ExamSession.objects.filter(subject=subject)
        self.fields["group"].queryset = StudyGroup.objects.filter(subject=subject)
        add_bootstrap_classes(self)

    def filter(self, attempts):
        """Сүзгіні әрекеттер тізіміне қолданады (сүзгі қате болса — тізім өзгермейді)."""
        if not self.is_valid():
            return attempts
        if self.cleaned_data["session"]:
            attempts = attempts.filter(session=self.cleaned_data["session"])
        if self.cleaned_data["group"]:
            attempts = attempts.filter(user__profile__group=self.cleaned_data["group"])
        return attempts


# ---------- Сұрақтар тізімінің сүзгісі ----------


class SampleVariantForm(forms.Form):
    """Үлгі нұсқаның тілі (GET ?lang=kk немесе ?lang=ru)."""

    lang = forms.ChoiceField(choices=Language.choices, required=False)

    def get_language(self):
        """Таңдалған тіл; бос немесе қате болса — қазақша."""
        if self.is_valid() and self.cleaned_data["lang"]:
            return self.cleaned_data["lang"]
        return Language.KK


class QuestionFilterForm(forms.Form):
    """
    Сұрақтар тізімінің сүзгісі (GET): тақырып, тақырыпша, деңгей, тіл, мәтіннен іздеу.
    Тақырыптар мен тақырыпшалар — тек оқытушының таңдалған пәнінен.
    """

    topic = forms.ModelChoiceField(
        label=_("Тақырып"),
        queryset=Topic.objects.none(),
        required=False,
        empty_label=_("Барлық тақырып"),
    )
    subtopic = forms.ModelChoiceField(
        label=_("Тақырыпша"),
        queryset=Subtopic.objects.none(),
        required=False,
        empty_label=_("Барлық тақырыпша"),
        widget=SubtopicSelect,
    )
    level = forms.ChoiceField(
        label=_("Деңгей"),
        choices=[("", _("Барлық деңгей"))] + Level.choices,
        required=False,
    )
    language = forms.ChoiceField(
        label=_("Тіл"),
        choices=[("", _("Барлық тіл"))] + Language.choices,
        required=False,
    )
    q = forms.CharField(label=_("Іздеу"), required=False)

    def __init__(self, *args, subject, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["topic"].queryset = Topic.objects.filter(subject=subject)
        self.fields["subtopic"].queryset = Subtopic.objects.filter(
            topic__subject=subject
        ).select_related("topic")
        add_bootstrap_classes(self)
        self.fields["q"].widget.attrs["placeholder"] = _("Мәтін немесе код")

    def filter(self, questions):
        """Сүзгіні сұрақтар тізіміне қолданады (сүзгі қате болса — тізім өзгермейді)."""
        if not self.is_valid():
            return questions
        data = self.cleaned_data
        if data["topic"]:
            questions = questions.filter(subtopic__topic=data["topic"])
        if data["subtopic"]:
            questions = questions.filter(subtopic=data["subtopic"])
        if data["level"]:
            questions = questions.filter(level=data["level"])
        if data["language"]:
            questions = questions.filter(language=data["language"])
        if data["q"]:
            questions = questions.filter(
                Q(text__icontains=data["q"]) | Q(code__icontains=data["q"])
            )
        return questions
