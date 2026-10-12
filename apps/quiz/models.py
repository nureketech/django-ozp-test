import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import StudyGroup

from .constants import IMAGE_EXTENSIONS, MAX_IMAGE_MB


class Language(models.TextChoices):
    """Сұрақ, контекст және тест тілі."""

    KK = "kk", _("Қазақша")
    RU = "ru", _("Орысша")


class Level(models.TextChoices):
    """Қиындық деңгейі (спецификация бойынша)."""

    A = "A", _("A — базалық")
    B = "B", _("B — орташа")
    C = "C", _("C — жоғары")


# Деңгей сипаттамалары (TZ.md, Б қосымшасы):
# формада қысқасы көрсетіледі, толығы подсказка (title) болып шығады
LEVEL_SHORT_DESCRIPTIONS = {
    "A": _(
        "A — базалық: қарапайым білім мен дағды, нұсқау бойынша әрекет, "
        "қарапайым ұғымдар"
    ),
    "B": _(
        "B — орташа: негізгі білімді жаңа жағдайда қолдану, деректерді талдау, "
        "салыстыру, жалпылау"
    ),
    "C": _(
        "C — жоғары: күрделі білімді біріктіру, күрделі деректерді талдау, "
        "тұжырымды негіздеу"
    ),
}
LEVEL_FULL_DESCRIPTIONS = {
    "A": _(
        "Базалық деңгейдегі тест тапсырмалары қарапайым білім мен дағдыларын "
        "пайдалануға, түсушінің ең төменгі дайындық деңгейіне баға беруге, белгілі "
        "бір нұсқаулардың көмегімен әрекеттерді орындауға, қарапайым дәлелдер мен "
        "ұғымдарды пайдалануға негізделген."
    ),
    "B": _(
        "Орташа деңгейдегі тест тапсырмалары негізгі білім мен дағдыларын дұрыс "
        "пайдалануға, жаңа жағдайларда қарапайым модельдерді тануға, деректерді "
        "талдау мен салыстыруға, жүйелеуге, дәлелдерді қолданып, ақпаратты жалпылау "
        "мен қорытынды жасау қабілеттерін бағалауға негізделген."
    ),
    "C": _(
        "Жоғары деңгейдегі тест тапсырмалары неғұрлым күрделі білім мен дағдыларын "
        "пайдалануды, тапсырмалардың күрделі модельдерін тануды, мәселелерді шешу "
        "үшін білім мен дағдыларын біріктіруді, күрделі ақпаратты немесе деректерді "
        "талдауды, пайымдауды, тұжырымдарды негіздеуге бағытталған."
    ),
}


def validate_image_size(image):
    """Сурет MAX_IMAGE_MB-тан (2 МБ) аспауы керек."""
    if image.size > MAX_IMAGE_MB * 1024 * 1024:
        raise ValidationError(
            _("Сурет тым үлкен: ең көбі %(size)s МБ."), params={"size": MAX_IMAGE_MB}
        )


# Сұрақ, контекст және жауап суреттерінің тексерулері: jpg / png / webp, 2 МБ-қа дейін
IMAGE_VALIDATORS = [FileExtensionValidator(IMAGE_EXTENSIONS), validate_image_size]


def random_image_name(folder, filename):
    """
    Сурет кездейсоқ атпен сақталады (folder/<uuid>.png): /media/ қорғалмаған,
    сондықтан «q1.png» сияқты атты тест басталмай тұрып табуға болмасын,
    ал жауап суретінің атынан дұрыс жауапты болжау мүмкін болмасын.
    """
    return f"{folder}/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"


def question_image_path(instance, filename):
    return random_image_name("questions", filename)


def context_image_path(instance, filename):
    return random_image_name("contexts", filename)


def answer_image_path(instance, filename):
    return random_image_name("answers", filename)


def localized_name(name_kk, name_ru):
    """Интерфейс тілі орысша болса — орысша атауды, әйтпесе қазақшасын қайтарады."""
    language = get_language() or ""
    if language.startswith("ru"):
        return name_ru
    return name_kk


# ---------- Пәндер ----------


class Subject(models.Model):
    """Пән (информатика, математика, ...), `load_subjects` арқылы жүктеледі."""

    code = models.SlugField(_("коды"), max_length=50, unique=True)
    name_kk = models.CharField(_("атауы (қаз)"), max_length=255)
    name_ru = models.CharField(_("атауы (орыс)"), max_length=255)
    # Тест уақыты (минут); сессияның жабылу уақытынан аспайды
    duration_minutes = models.PositiveSmallIntegerField(_("тест уақыты (минут)"))
    uses_formulas = models.BooleanField(_("формулалар қолданылады"), default=False)
    order = models.PositiveSmallIntegerField(_("реті"), default=0)
    is_active = models.BooleanField(_("белсенді"), default=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = _("пән")
        verbose_name_plural = _("пәндер")

    def __str__(self):
        return self.name

    @property
    def name(self):
        return localized_name(self.name_kk, self.name_ru)


# ---------- Сұрақтар банкі ----------


class Topic(models.Model):
    """Тақырып; нөмірі пән ішінде бірегей. `load_subjects` арқылы жүктеледі."""

    subject = models.ForeignKey(
        Subject,
        on_delete=models.PROTECT,
        related_name="topics",
        verbose_name=_("пән"),
    )
    number = models.PositiveSmallIntegerField(_("нөмірі"))
    name_kk = models.CharField(_("атауы (қаз)"), max_length=255)
    name_ru = models.CharField(_("атауы (орыс)"), max_length=255)

    class Meta:
        ordering = ["subject__order", "number"]
        verbose_name = _("тақырып")
        verbose_name_plural = _("тақырыптар")
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "number"], name="unique_topic_number_in_subject"
            ),
        ]

    def __str__(self):
        return f"{self.number:02d} {self.name}"

    @property
    def name(self):
        return localized_name(self.name_kk, self.name_ru)


class Subtopic(models.Model):
    """
    Тақырыпша (01–20); нөмірі пән ішінде бірегей (DB шектеуі жоқ,
    `load_subjects` пен тесттер тексереді).
    """

    topic = models.ForeignKey(
        Topic,
        on_delete=models.PROTECT,
        related_name="subtopics",
        verbose_name=_("тақырып"),
    )
    number = models.PositiveSmallIntegerField(_("нөмірі"))
    name_kk = models.CharField(_("атауы (қаз)"), max_length=255)
    name_ru = models.CharField(_("атауы (орыс)"), max_length=255)
    # Спецификациядағы толық мазмұны (сұрақ формасында подсказка)
    description_kk = models.TextField(_("сипаттамасы (қаз)"), blank=True)

    class Meta:
        ordering = ["topic__subject__order", "number"]
        verbose_name = _("тақырыпша")
        verbose_name_plural = _("тақырыпшалар")

    def __str__(self):
        return f"{self.number:02d} {self.name}"

    @property
    def name(self):
        return localized_name(self.name_kk, self.name_ru)


class Context(models.Model):
    """Контекст (мәтін, кесте, график, сурет), оған 5 сұрақ байланады."""

    subject = models.ForeignKey(
        Subject,
        on_delete=models.PROTECT,
        related_name="contexts",
        verbose_name=_("пән"),
    )
    language = models.CharField(_("тілі"), max_length=2, choices=Language.choices)
    title = models.CharField(_("атауы"), max_length=255)
    text = models.TextField(_("мәтіні"))
    # Бағдарлама коды: мәтін өзгертілмей сақталады, <pre> ішінде көрсетіледі
    code = models.TextField(_("коды"), blank=True)
    image = models.ImageField(
        _("суреті"), upload_to=context_image_path, blank=True, validators=IMAGE_VALIDATORS
    )
    is_demo = models.BooleanField(_("демо"), default=False)
    is_active = models.BooleanField(_("белсенді"), default=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = _("контекст")
        verbose_name_plural = _("контексттер")

    def __str__(self):
        return f"[{self.language}] {self.title}"

    def active_questions_count(self):
        """Контекстке байланған белсенді сұрақтар саны (толық контекстте — 5)."""
        return self.questions.filter(is_active=True).count()


class Question(models.Model):
    """Сұрақ. Тақырыбы мен пәні `subtopic.topic.subject` арқылы анықталады."""

    subtopic = models.ForeignKey(
        Subtopic,
        on_delete=models.PROTECT,
        related_name="questions",
        verbose_name=_("тақырыпша"),
    )
    context = models.ForeignKey(
        Context,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="questions",
        verbose_name=_("контекст"),
    )
    language = models.CharField(_("тілі"), max_length=2, choices=Language.choices)
    text = models.TextField(_("сұрақ мәтіні"))
    image = models.ImageField(
        _("суреті"), upload_to=question_image_path, blank=True, validators=IMAGE_VALIDATORS
    )
    # Бағдарлама коды: шегіністер сақталады, <pre> ішінде көрсетіледі
    code = models.TextField(_("коды"), blank=True)
    level = models.CharField(_("деңгейі"), max_length=1, choices=Level.choices)
    is_demo = models.BooleanField(_("демо"), default=False)
    is_active = models.BooleanField(_("белсенді"), default=True)

    class Meta:
        ordering = ["subtopic__number", "id"]
        verbose_name = _("сұрақ")
        verbose_name_plural = _("сұрақтар")

    def __str__(self):
        short_text = self.text[:60]
        return f"{self.subtopic.number:02d}/{self.level}/{self.language}: {short_text}"

    def clean(self):
        if not self.context_id:
            return
        # Контекстке байланған сұрақ контекстпен бір тілде болуы керек
        if self.context.language != self.language:
            raise ValidationError(
                {"context": _("Контекст пен сұрақтың тілі бірдей болуы керек.")}
            )
        # ... және бір пәнде
        if self.subtopic_id and self.context.subject_id != self.subtopic.topic.subject_id:
            raise ValidationError(
                {"context": _("Контекст пен сұрақтың пәні бірдей болуы керек.")}
            )


class Answer(models.Model):
    """
    Жауап нұсқасы: мәтін немесе сурет (екеуі де болуы мүмкін). Әр сұрақта дәл
    4 нұсқа, біреуі дұрыс, әрқайсында мәтін не сурет бар (forms.py тексереді).
    """

    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name="answers",
        verbose_name=_("сұрақ"),
    )
    # Мәтін өзгертілмей сақталады, <pre> ішінде көрсетіледі (код болуы мүмкін)
    text = models.TextField(_("мәтіні"), blank=True)
    image = models.ImageField(
        _("суреті"), upload_to=answer_image_path, blank=True, validators=IMAGE_VALIDATORS
    )
    is_correct = models.BooleanField(_("дұрыс"), default=False)

    class Meta:
        ordering = ["id"]
        verbose_name = _("жауап нұсқасы")
        verbose_name_plural = _("жауап нұсқалары")

    def __str__(self):
        # Тек суреттен тұратын нұсқа
        return self.text[:60] or f"[{Path(self.image.name).name}]"


# ---------- Сессиялар ----------


class ExamSession(models.Model):
    """Оқытушы белгілеген уақытта ғана ашылатын тест сессиясы."""

    class ShowAnswers(models.TextChoices):
        AFTER_FINISH = "after_finish", _("Тест аяқталған бойда")
        AFTER_CLOSE = "after_close", _("Сессия жабылғаннан кейін")

    title = models.CharField(_("атауы"), max_length=255)
    subject = models.ForeignKey(
        Subject,
        on_delete=models.PROTECT,
        related_name="exam_sessions",
        verbose_name=_("пән"),
    )
    opens_at = models.DateTimeField(_("ашылу уақыты"))
    closes_at = models.DateTimeField(_("жабылу уақыты"))
    # Бос болса — сессия осы пәннің барлық тобына арналған
    groups = models.ManyToManyField(
        StudyGroup,
        blank=True,
        related_name="exam_sessions",
        verbose_name=_("топтар"),
    )
    show_answers = models.CharField(
        _("дұрыс жауаптарды көрсету"),
        max_length=20,
        choices=ShowAnswers.choices,
        default=ShowAnswers.AFTER_FINISH,
    )
    is_active = models.BooleanField(_("белсенді"), default=True)

    class Meta:
        ordering = ["-opens_at"]
        verbose_name = _("тест сессиясы")
        verbose_name_plural = _("тест сессиялары")

    def __str__(self):
        return self.title

    def clean(self):
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValidationError(
                {"closes_at": _("Жабылу уақыты ашылу уақытынан кейін болуы керек.")}
            )


# ---------- Тест әрекеттері ----------


class Attempt(models.Model):
    """Студенттің бір сессиядағы тест әрекеті (бір сессияда — бір рет)."""

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", _("Жүріп жатыр")
        FINISHED = "finished", _("Аяқталды")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="attempts",
        verbose_name=_("студент"),
    )
    session = models.ForeignKey(
        ExamSession,
        on_delete=models.PROTECT,
        related_name="attempts",
        verbose_name=_("сессия"),
    )
    language = models.CharField(_("тест тілі"), max_length=2, choices=Language.choices)
    started_at = models.DateTimeField(_("басталған уақыты"), default=timezone.now)
    # deadline = min(started_at + session.subject.duration_minutes, session.closes_at)
    # — services.py есептейді
    deadline = models.DateTimeField(_("мерзімі"))
    finished_at = models.DateTimeField(_("аяқталған уақыты"), null=True, blank=True)
    score = models.PositiveSmallIntegerField(_("балы"), null=True, blank=True)
    status = models.CharField(
        _("күйі"),
        max_length=20,
        choices=Status.choices,
        default=Status.IN_PROGRESS,
    )

    class Meta:
        ordering = ["-started_at"]
        verbose_name = _("тест әрекеті")
        verbose_name_plural = _("тест әрекеттері")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "session"], name="unique_attempt_per_session"
            ),
        ]

    def __str__(self):
        return f"{self.user} — {self.session}"


class AttemptQuestion(models.Model):
    """Нұсқадағы бір сұрақ: реті, нұсқалардың араласқан реті және студент жауабы."""

    attempt = models.ForeignKey(
        Attempt,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("тест әрекеті"),
    )
    question = models.ForeignKey(
        Question,
        on_delete=models.PROTECT,
        related_name="attempt_items",
        verbose_name=_("сұрақ"),
    )
    order = models.PositiveSmallIntegerField(_("реті"))
    # Жауап нұсқаларының id тізімі — студентке осы ретпен көрсетіледі (A, B, C, D)
    answer_order = models.JSONField(_("нұсқалар реті"), default=list)
    # Дұрыс/қате бөлек сақталмайды: selected.is_correct арқылы есептеледі
    selected = models.ForeignKey(
        Answer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("таңдалған жауап"),
    )

    class Meta:
        ordering = ["order"]
        verbose_name = _("нұсқадағы сұрақ")
        verbose_name_plural = _("нұсқадағы сұрақтар")
        constraints = [
            models.UniqueConstraint(
                fields=["attempt", "order"], name="unique_order_in_attempt"
            ),
        ]

    def __str__(self):
        return f"{self.attempt} — №{self.order}"
