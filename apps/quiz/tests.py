import atexit
import os
import random
import re
import shutil
import tempfile
from collections import Counter
from datetime import timedelta
from io import BytesIO, StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.forms import inlineformset_factory
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.translation import override
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image

from apps.accounts.models import StudyGroup

from .constants import (
    ANSWERS_PER_QUESTION,
    PRACTICE_QUESTIONS,
    CONTEXTS_PER_TEST,
    LEVEL_QUOTA,
    MIN_CONTEXTS,
    MIN_QUESTIONS_PER_SUBTOPIC,
    QUESTIONS_PER_CONTEXT,
    QUESTIONS_TOTAL,
    SINGLE_QUESTIONS_PER_SUBTOPIC,
    INFORMATICS_CODE,
    MAX_IMAGE_MB,
    SUBTOPICS_COUNT,
)
from .forms import AnswerInlineFormSet, ExamSessionAdminForm
from .management.commands.load_subjects import read_subjects
from .management.commands.loadtest_data import DEFAULT_PASSWORD as LOADTEST_PASSWORD
from .management.commands.loadtest_data import SESSION_TITLE as LOADTEST_SESSION_TITLE
from .management.commands.loadtest_data import usernames as loadtest_usernames
from .models import (
    LEVEL_FULL_DESCRIPTIONS,
    LEVEL_SHORT_DESCRIPTIONS,
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
from .question_import import CONTEXTS_SHEET, QUESTIONS_SHEET
from .services import (
    AttemptError,
    attempt_deadline,
    bank_coverage,
    build_variant,
    can_see_answers,
    create_attempt,
    csv_safe,
    dashboard_sessions,
    finish_attempt,
    finish_expired_attempts,
    is_session_open,
    remaining_seconds,
    save_answer,
    split_duration,
    student_language,
    visible_sessions,
)
from .teacher_views import XLSX_CONTENT_TYPE


# Информатиканың тест уақыты (TZ.md, 10.1)
INFORMATICS_MINUTES = 125


def informatics():
    """Деректер миграциясы жасаған «Информатика» пәні."""
    return Subject.objects.get(code=INFORMATICS_CODE)


def make_teacher(username="teacher", subjects=None):
    """Оқытушы (is_staff); әдепкіде оған «Информатика» тағайындалады."""
    teacher = User.objects.create_user(username=username, password="pass12345", is_staff=True)
    teacher.profile.subjects.set(subjects if subjects is not None else [informatics()])
    return teacher


def make_student(username="student", group=None, **extra):
    """Студент; әдепкіде «ИНФ-21» (информатика) тобында — студенттің пәні тобынан."""
    if group is None:
        group, _created = StudyGroup.objects.get_or_create(
            name="ИНФ-21", defaults={"subject": informatics(), "language": "kk"}
        )
    student = User.objects.create_user(username=username, password="pass12345", **extra)
    student.profile.group = group
    student.profile.save()
    return student


def run_command(*args):
    """Команданы шығысын жасырып іске қосады."""
    call_command(*args, stdout=StringIO())


def make_session(**kwargs):
    now = timezone.now()
    data = {
        "title": "Сынақ сессия",
        "subject": informatics(),
        "opens_at": now - timedelta(hours=1),
        "closes_at": now + timedelta(hours=3),
    }
    data.update(kwargs)
    return ExamSession.objects.create(**data)


class LoadTopicsTests(TestCase):
    def test_loads_11_topics_and_20_subtopics(self):
        run_command("load_topics")
        self.assertEqual(Topic.objects.count(), 11)
        self.assertEqual(Subtopic.objects.count(), SUBTOPICS_COUNT)

    def test_is_idempotent(self):
        run_command("load_topics")
        run_command("load_topics")
        self.assertEqual(Topic.objects.count(), 11)
        self.assertEqual(Subtopic.objects.count(), SUBTOPICS_COUNT)

    def test_subtopics_belong_to_correct_topics(self):
        run_command("load_topics")
        topic_2 = Topic.objects.get(number=2)
        self.assertEqual(list(topic_2.subtopics.values_list("number", flat=True)), [3, 4])
        topic_8 = Topic.objects.get(number=8)
        self.assertEqual(
            list(topic_8.subtopics.values_list("number", flat=True)), [13, 14, 15, 16]
        )

    def test_name_follows_interface_language(self):
        run_command("load_topics")
        topic = Topic.objects.get(number=3)
        with override("kk"):
            self.assertEqual(topic.name, "Логикалық операциялар")
            self.assertEqual(str(topic), "03 Логикалық операциялар")
        with override("ru"):
            self.assertEqual(topic.name, "Логические операции")


class LoadDemoTests(TestCase):
    def setUp(self):
        run_command("load_topics")
        run_command("load_demo")

    def test_requires_topics(self):
        Question.objects.all().delete()
        Context.objects.all().delete()
        Subtopic.objects.all().delete()
        with self.assertRaises(CommandError):
            run_command("load_demo")

    def test_all_demo_data_is_marked(self):
        self.assertFalse(Question.objects.filter(is_demo=False).exists())
        self.assertFalse(Context.objects.filter(is_demo=False).exists())

    def test_every_question_has_4_answers_and_one_correct(self):
        for question in Question.objects.prefetch_related("answers"):
            answers = list(question.answers.all())
            self.assertEqual(len(answers), ANSWERS_PER_QUESTION)
            self.assertEqual(sum(answer.is_correct for answer in answers), 1)

    def test_bank_is_big_enough_for_each_language(self):
        for language in ["kk", "ru"]:
            single = Question.objects.filter(
                language=language, context__isnull=True, is_active=True
            )
            per_subtopic = Counter(single.values_list("subtopic__number", flat=True))
            self.assertEqual(len(per_subtopic), SUBTOPICS_COUNT)
            self.assertGreaterEqual(min(per_subtopic.values()), MIN_QUESTIONS_PER_SUBTOPIC)

            # Әр деңгейдің сұрақтары квотадан кем емес
            per_level = Counter(single.values_list("level", flat=True))
            for level, quota in LEVEL_QUOTA.items():
                self.assertGreaterEqual(per_level[level], quota)

            contexts = Context.objects.filter(language=language, is_active=True)
            self.assertGreaterEqual(contexts.count(), MIN_CONTEXTS)
            for context in contexts:
                questions = context.questions.all()
                self.assertEqual(questions.count(), QUESTIONS_PER_CONTEXT)
                self.assertFalse(questions.exclude(language=language).exists())

    def test_code_indentation_is_preserved(self):
        question = Question.objects.filter(code__contains="return n * f(n - 2)").first()
        self.assertIn("\n    if n <= 1:\n        return 1\n", question.code)
        answer = Answer.objects.filter(text__startswith="if x % 2 == 0:").first()
        self.assertEqual(answer.text, 'if x % 2 == 0:\n    print("Yes")')

    def test_second_run_does_not_duplicate(self):
        count = Question.objects.count()
        run_command("load_demo")
        self.assertEqual(Question.objects.count(), count)

    def test_delete_removes_only_demo_data(self):
        real_question = Question.objects.create(
            subtopic=Subtopic.objects.get(number=1),
            language="kk",
            text="Нақты сұрақ",
            level="A",
        )
        # Демо сұрақ кездесетін тест әрекеті де өшуі керек
        user = User.objects.create_user(username="student", password="pass12345")
        session = make_session()
        attempt = Attempt.objects.create(
            user=user, session=session, language="kk", deadline=session.closes_at
        )
        demo_question = Question.objects.filter(is_demo=True).first()
        AttemptQuestion.objects.create(attempt=attempt, question=demo_question, order=1)

        run_command("load_demo", "--delete")

        self.assertFalse(Question.objects.filter(is_demo=True).exists())
        self.assertFalse(Context.objects.filter(is_demo=True).exists())
        self.assertFalse(Attempt.objects.exists())
        self.assertEqual(list(Question.objects.all()), [real_question])
        self.assertEqual(Answer.objects.count(), 0)


class ModelTests(TestCase):
    def setUp(self):
        run_command("load_topics")
        self.subtopic = Subtopic.objects.get(number=5)

    def test_attempt_is_unique_per_user_and_session(self):
        user = User.objects.create_user(username="student", password="pass12345")
        session = make_session()
        Attempt.objects.create(user=user, session=session, language="kk", deadline=session.closes_at)
        with self.assertRaises(IntegrityError):
            Attempt.objects.create(
                user=user, session=session, language="ru", deadline=session.closes_at
            )

    def test_session_must_close_after_opening(self):
        now = timezone.now()
        session = ExamSession(title="Қате", opens_at=now, closes_at=now - timedelta(hours=1))
        with self.assertRaises(ValidationError):
            session.full_clean()

    def test_context_question_language_must_match(self):
        context = Context.objects.create(
            subject=informatics(), language="ru", title="Контекст", text="Мәтін"
        )
        question = Question(
            subtopic=self.subtopic, context=context, language="kk", text="Сұрақ", level="B"
        )
        with self.assertRaises(ValidationError):
            question.full_clean()


class AnswerFormSetTests(TestCase):
    """Дәл 4 нұсқа және дәл бір дұрыс жауап тексерісі."""

    def setUp(self):
        run_command("load_topics")
        self.question = Question.objects.create(
            subtopic=Subtopic.objects.get(number=1), language="kk", text="Сұрақ", level="A"
        )
        self.formset_class = inlineformset_factory(
            Question,
            Answer,
            formset=AnswerInlineFormSet,
            fields=["text", "is_correct"],
            extra=ANSWERS_PER_QUESTION,
        )

    def make_formset(self, answers):
        """answers: [(мәтін, дұрыс па), ...] — бос жолдар толтырылмайды."""
        data = {
            "answers-TOTAL_FORMS": str(ANSWERS_PER_QUESTION),
            "answers-INITIAL_FORMS": "0",
        }
        for index, (text, is_correct) in enumerate(answers):
            data[f"answers-{index}-text"] = text
            if is_correct:
                data[f"answers-{index}-is_correct"] = "on"
        return self.formset_class(data, instance=self.question, prefix="answers")

    def test_valid_with_4_answers_and_one_correct(self):
        formset = self.make_formset([("1", True), ("2", False), ("3", False), ("4", False)])
        self.assertTrue(formset.is_valid(), formset.non_form_errors())

    def test_invalid_with_3_answers(self):
        formset = self.make_formset([("1", True), ("2", False), ("3", False)])
        self.assertFalse(formset.is_valid())

    def test_invalid_with_two_correct(self):
        formset = self.make_formset([("1", True), ("2", True), ("3", False), ("4", False)])
        self.assertFalse(formset.is_valid())

    def test_invalid_without_correct(self):
        formset = self.make_formset([("1", False), ("2", False), ("3", False), ("4", False)])
        self.assertFalse(formset.is_valid())


class PageTests(TestCase):
    def test_home_page_in_kazakh_by_default(self):
        response = self.client.get(reverse("quiz:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'lang="kk"')
        self.assertContains(response, "ПББ тестіне дайындық")

    def test_language_switch_is_saved(self):
        response = self.client.post(
            reverse("set_language"), {"language": "ru", "next": "/"}
        )
        self.assertRedirects(response, "/")
        response = self.client.get(reverse("quiz:home"))
        self.assertContains(response, 'lang="ru"')

    def test_admin_pages_open_for_superuser(self):
        run_command("load_topics")
        run_command("load_demo")
        admin = User.objects.create_superuser(username="admin", password="pass12345")
        self.client.force_login(admin)
        question = Question.objects.first()
        urls = [
            reverse("admin:index"),
            reverse("admin:quiz_topic_changelist"),
            reverse("admin:quiz_subtopic_changelist"),
            reverse("admin:quiz_question_changelist"),
            reverse("admin:quiz_question_add"),
            reverse("admin:quiz_question_change", args=[question.pk]),
            reverse("admin:quiz_context_changelist"),
            reverse("admin:quiz_examsession_changelist"),
            reverse("admin:quiz_examsession_add"),
            reverse("admin:quiz_attempt_changelist"),
            reverse("admin:accounts_studygroup_changelist"),
            reverse("admin:auth_user_changelist"),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


# ---------- 2-кезең: оқытушы беттері ----------


class TeacherTestCase(TestCase):
    """Оқытушы беттерінің ортақ дайындығы: тақырыптар және кірген оқытушы."""

    def setUp(self):
        run_command("load_topics")
        self.teacher = make_teacher()
        self.client.force_login(self.teacher)
        self.topic = Topic.objects.get(number=2)
        self.subtopic = Subtopic.objects.get(number=3)  # 02 тақырыптікі

    def question_data(self, **overrides):
        """Сұрақ формасының дұрыс толтырылған POST деректері."""
        data = {
            "topic": self.topic.pk,
            "subtopic": self.subtopic.pk,
            "level": "B",
            "language": "kk",
            "context": "",
            "text": "Алғашқы электрондық есептеуіш машина қалай аталады?",
            "code": "",
            "answers-TOTAL_FORMS": "4",
            "answers-INITIAL_FORMS": "0",
            "answers-MIN_NUM_FORMS": "4",
            "answers-MAX_NUM_FORMS": "4",
            "answers-0-text": "ENIAC",
            "answers-1-text": "IBM PC",
            "answers-2-text": "Apple I",
            "answers-3-text": "Altair 8800",
            "answers-correct": "0",
            "save": "",
        }
        data.update(overrides)
        return data

    def create_question(self, **kwargs):
        """Дерекқорға 4 жауабы бар сұрақ жазады (бірінші жауап — дұрыс)."""
        data = {"subtopic": self.subtopic, "language": "kk", "text": "Сұрақ", "level": "A"}
        data.update(kwargs)
        question = Question.objects.create(**data)
        for index in range(ANSWERS_PER_QUESTION):
            Answer.objects.create(
                question=question, text=f"Жауап {index}", is_correct=index == 0
            )
        return question

    def create_context(self, language="kk"):
        return Context.objects.create(
            subject=informatics(), language=language, title="Кесте", text="Мәтін"
        )


class TeacherAccessTests(TeacherTestCase):
    def teacher_urls(self):
        question = self.create_question()
        quiz_context = self.create_context()
        return [
            reverse("quiz:teacher_questions"),
            reverse("quiz:teacher_question_create"),
            reverse("quiz:teacher_question_edit", args=[question.pk]),
            reverse("quiz:teacher_contexts"),
            reverse("quiz:teacher_context_create"),
            reverse("quiz:teacher_context_edit", args=[quiz_context.pk]),
            reverse("quiz:teacher_bank"),
        ]

    def test_pages_open_for_teacher(self):
        for url in self.teacher_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_student_and_guest_are_redirected_to_login(self):
        urls = self.teacher_urls()
        student = User.objects.create_user(username="student", password="pass12345")
        for user in [student, None]:
            self.client.logout()
            if user:
                self.client.force_login(user)
            for url in urls:
                with self.subTest(url=url, user=user):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn(reverse("admin:login"), response["Location"])

    def test_pages_open_in_russian(self):
        self.client.cookies["django_language"] = "ru"
        for url in self.teacher_urls():
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), 'lang="ru"')


class QuestionFormPageTests(TeacherTestCase):
    def post_question(self, url=None, **overrides):
        url = url or reverse("quiz:teacher_question_create")
        return self.client.post(url, self.question_data(**overrides))

    def test_create_question_with_answers(self):
        response = self.post_question(**{"answers-correct": "2"})
        question = Question.objects.get()
        self.assertRedirects(
            response, reverse("quiz:teacher_question_edit", args=[question.pk])
        )
        self.assertEqual(question.subtopic, self.subtopic)
        self.assertEqual(question.level, "B")
        self.assertFalse(question.is_demo)
        answers = list(question.answers.all())
        self.assertEqual(
            [answer.text for answer in answers],
            ["ENIAC", "IBM PC", "Apple I", "Altair 8800"],
        )
        self.assertEqual(
            [answer.is_correct for answer in answers], [False, False, True, False]
        )

    def test_subtopic_from_other_topic_is_rejected(self):
        other_subtopic = Subtopic.objects.get(number=5)  # 03 тақырыптікі
        response = self.post_question(subtopic=other_subtopic.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Question.objects.exists())
        self.assertIn("subtopic", response.context["form"].errors)
        self.assertContains(response, "Бұл тақырыпша таңдалған тақырыпқа жатпайды.")

    def test_required_fields(self):
        for field in ["topic", "subtopic", "level", "language", "text"]:
            with self.subTest(field=field):
                response = self.post_question(**{field: ""})
                self.assertIn(field, response.context["form"].errors)
        self.assertFalse(Question.objects.exists())

    def test_code_and_image_are_optional(self):
        self.post_question(code="")
        question = Question.objects.get()
        self.assertEqual(question.code, "")
        self.assertFalse(question.image)

    def test_empty_answer_is_rejected(self):
        response = self.post_question(**{"answers-3-text": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Question.objects.exists())
        self.assertTrue(response.context["formset"].forms[3].errors)

    def test_correct_answer_is_required(self):
        data = self.question_data()
        del data["answers-correct"]
        response = self.client.post(reverse("quiz:teacher_question_create"), data)
        self.assertFalse(Question.objects.exists())
        self.assertContains(response, "Дұрыс жауапты белгілеңіз.")

    def test_more_than_4_answers_is_rejected(self):
        response = self.post_question(
            **{"answers-TOTAL_FORMS": "5", "answers-4-text": "Артық", "answers-correct": "4"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Question.objects.exists())

    def test_code_indentation_is_preserved(self):
        code = (
            "def f(n):\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * f(n - 2)\n"
            "\n"
            'print(f(5), "end")'
        )
        # Бірінші жолдың шегінісі мен табуляция да сақталуы керек
        answer_code = '    if x > 0:\n\tprint("Иә")'
        # Браузер textarea мәтінін \r\n жол соңымен жібереді
        self.post_question(
            code=code.replace("\n", "\r\n"),
            **{"answers-1-text": answer_code.replace("\n", "\r\n")},
        )
        question = Question.objects.get()
        self.assertEqual(question.code, code)
        self.assertEqual(question.answers.all()[1].text, answer_code)

        # Алдын ала қарауда код <pre> ішінде өзгеріссіз көрсетіледі
        response = self.client.get(
            reverse("quiz:teacher_question_edit", args=[question.pk])
        )
        self.assertContains(response, f'<pre class="code-block">{escape(code)}</pre>')
        self.assertContains(
            response, f'<pre class="answer-text">{escape(answer_code)}</pre>'
        )
        # Өңдеу формасының textarea өрісінде де шегіністер сақталады
        self.assertContains(response, f">\n{escape(code)}</textarea>")

    def test_save_and_next_keeps_selection(self):
        data = self.question_data(level="C", language="ru", save_and_next="")
        del data["save"]
        response = self.client.post(reverse("quiz:teacher_question_create"), data)
        expected = reverse("quiz:teacher_question_create") + (
            f"?topic={self.topic.pk}&subtopic={self.subtopic.pk}&level=C&language=ru"
        )
        self.assertRedirects(response, expected)

        form = self.client.get(expected).context["form"]
        self.assertEqual(form["topic"].value(), str(self.topic.pk))
        self.assertEqual(form["subtopic"].value(), str(self.subtopic.pk))
        self.assertEqual(form["level"].value(), "C")
        self.assertEqual(form["language"].value(), "ru")
        self.assertIsNone(form["text"].value())

    def test_edit_changes_only_correct_answer(self):
        question = self.create_question()
        answer_ids = list(question.answers.values_list("pk", flat=True))
        data = self.question_data(**{"answers-INITIAL_FORMS": "4", "answers-correct": "3"})
        for index, answer in enumerate(question.answers.all()):
            data[f"answers-{index}-id"] = answer.pk
            data[f"answers-{index}-text"] = answer.text
        url = reverse("quiz:teacher_question_edit", args=[question.pk])
        response = self.client.post(url, data)
        self.assertRedirects(response, url)
        answers = list(question.answers.all())
        self.assertEqual([answer.pk for answer in answers], answer_ids)
        self.assertEqual(
            [answer.is_correct for answer in answers], [False, False, False, True]
        )

    def use_in_attempt(self, question):
        """Сұрақты студенттің тест әрекетіне қосады (сұрақ «қолданылған» болады)."""
        now = timezone.now()
        attempt = Attempt.objects.create(
            user=make_student(),
            session=make_session(),
            language="kk",
            deadline=now + timedelta(hours=1),
        )
        AttemptQuestion.objects.create(
            attempt=attempt,
            question=question,
            order=1,
            answer_order=list(question.answers.values_list("pk", flat=True)),
        )

    def edit_data(self, question, correct, texts=None):
        """Сұрақты өңдеу формасының POST деректері (correct — дұрыс нұсқаның реті)."""
        data = self.question_data(
            **{"answers-INITIAL_FORMS": "4", "answers-correct": str(correct)}
        )
        for index, answer in enumerate(question.answers.all()):
            data[f"answers-{index}-id"] = answer.pk
            data[f"answers-{index}-text"] = texts[index] if texts else answer.text
        return data

    def test_used_question_correct_answer_cannot_change(self):
        question = self.create_question()
        self.use_in_attempt(question)
        url = reverse("quiz:teacher_question_edit", args=[question.pk])
        response = self.client.post(url, self.edit_data(question, correct=3))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "дұрыс жауабын өзгертуге болмайды")
        self.assertEqual(
            [answer.is_correct for answer in question.answers.all()],
            [True, False, False, False],
        )

    def test_used_question_text_can_be_fixed(self):
        question = self.create_question()
        self.use_in_attempt(question)
        url = reverse("quiz:teacher_question_edit", args=[question.pk])
        texts = ["Түзетілген", "Жауап 1", "Жауап 2", "Жауап 3"]
        response = self.client.post(url, self.edit_data(question, correct=0, texts=texts))
        self.assertRedirects(response, url)
        answers = list(question.answers.all())
        self.assertEqual([answer.text for answer in answers], texts)
        self.assertEqual([answer.is_correct for answer in answers], [True, False, False, False])

    def test_used_question_edit_page_shows_warning(self):
        question = self.create_question()
        url = reverse("quiz:teacher_question_edit", args=[question.pk])
        self.assertNotContains(self.client.get(url), "тестінде қолданылған")
        self.use_in_attempt(question)
        self.assertContains(self.client.get(url), "тестінде қолданылған")

    def test_edit_form_shows_topic_and_correct_answer(self):
        question = self.create_question(level="C")
        response = self.client.get(
            reverse("quiz:teacher_question_edit", args=[question.pk])
        )
        self.assertEqual(response.context["form"]["topic"].value(), self.topic.pk)
        self.assertEqual(response.context["formset"].correct_value(), "0")
        self.assertRegex(response.content.decode(), r'value="0"\s+checked')

    def test_subtopic_options_have_topic(self):
        response = self.client.get(reverse("quiz:teacher_question_create"))
        self.assertContains(
            response, f'<option value="{self.subtopic.pk}" data-topic="{self.topic.pk}">'
        )

    def test_level_shows_short_and_full_description(self):
        response = self.client.get(reverse("quiz:teacher_question_create"))
        for level in ["A", "B", "C"]:
            with self.subTest(level=level):
                self.assertContains(response, LEVEL_SHORT_DESCRIPTIONS[level])
                self.assertContains(response, f'title="{LEVEL_FULL_DESCRIPTIONS[level]}"')

    def test_context_language_must_match(self):
        quiz_context = self.create_context(language="ru")
        response = self.post_question(context=quiz_context.pk)
        self.assertFalse(Question.objects.exists())
        self.assertIn("context", response.context["form"].errors)

    def test_context_cannot_have_more_than_5_questions(self):
        quiz_context = self.create_context()
        for _ in range(QUESTIONS_PER_CONTEXT):
            self.create_question(context=quiz_context)
        response = self.post_question(context=quiz_context.pk)
        self.assertIn("context", response.context["form"].errors)
        self.assertEqual(quiz_context.questions.count(), QUESTIONS_PER_CONTEXT)

    def test_add_question_from_context_page(self):
        quiz_context = self.create_context(language="ru")
        url = reverse("quiz:teacher_question_create") + (
            f"?context={quiz_context.pk}&language=ru"
        )
        response = self.client.get(
            reverse("quiz:teacher_context_edit", args=[quiz_context.pk])
        )
        self.assertContains(response, url.replace("&", "&amp;"))

        form = self.client.get(url).context["form"]
        self.assertEqual(form["context"].value(), str(quiz_context.pk))
        self.assertEqual(form["language"].value(), "ru")

        # «Сақтап, келесісін қосу» контексті де сақтайды (онда әлі орын бар)
        response = self.post_question(
            language="ru", context=quiz_context.pk, save_and_next=""
        )
        self.assertEqual(quiz_context.questions.count(), 1)
        self.assertIn(f"context={quiz_context.pk}", response["Location"])


class QuestionListTests(TeacherTestCase):
    def setUp(self):
        super().setUp()
        self.question_a = self.create_question(text="Процессор жиілігі", level="A")
        self.question_b = self.create_question(
            text="Тізімді сұрыптау",
            level="B",
            language="ru",
            subtopic=Subtopic.objects.get(number=15),
            code="a.sort()",
        )

    def list_questions(self, **params):
        response = self.client.get(reverse("quiz:teacher_questions"), params)
        return list(response.context["page"].object_list)

    def test_filters(self):
        self.assertEqual(self.list_questions(), [self.question_a, self.question_b])
        self.assertEqual(self.list_questions(topic=self.topic.pk), [self.question_a])
        self.assertEqual(self.list_questions(subtopic=self.subtopic.pk), [self.question_a])
        self.assertEqual(self.list_questions(level="B"), [self.question_b])
        self.assertEqual(self.list_questions(language="ru"), [self.question_b])
        self.assertEqual(self.list_questions(q="Процессор"), [self.question_a])
        self.assertEqual(self.list_questions(q="sort"), [self.question_b])

    def test_copy_question(self):
        response = self.client.post(
            reverse("quiz:teacher_question_copy", args=[self.question_b.pk])
        )
        copy = Question.objects.latest("id")
        self.assertRedirects(response, reverse("quiz:teacher_question_edit", args=[copy.pk]))
        self.assertNotEqual(copy.pk, self.question_b.pk)
        self.assertEqual(
            (copy.subtopic, copy.text, copy.code, copy.level, copy.language),
            (self.question_b.subtopic, "Тізімді сұрыптау", "a.sort()", "B", "ru"),
        )
        self.assertEqual(
            list(copy.answers.values_list("text", "is_correct")),
            list(self.question_b.answers.values_list("text", "is_correct")),
        )

    def test_copy_of_full_context_question_becomes_single(self):
        quiz_context = self.create_context()
        questions = [
            self.create_question(context=quiz_context) for _ in range(QUESTIONS_PER_CONTEXT)
        ]
        self.client.post(reverse("quiz:teacher_question_copy", args=[questions[0].pk]))
        self.assertIsNone(Question.objects.latest("id").context)
        self.assertEqual(quiz_context.active_questions_count(), QUESTIONS_PER_CONTEXT)

    def test_toggle_active(self):
        url = reverse("quiz:teacher_question_toggle", args=[self.question_a.pk])
        next_url = reverse("quiz:teacher_questions") + "?level=A"
        response = self.client.post(url, {"next": next_url})
        self.assertRedirects(response, next_url)
        self.question_a.refresh_from_db()
        self.assertFalse(self.question_a.is_active)
        # Белсенді емес сұрақ жойылмайды және тізімде қалады
        self.assertIn(self.question_a, self.list_questions())

        # Бөтен сайтқа қайта бағыттамайды
        response = self.client.post(url, {"next": "https://evil.example.com/"})
        self.assertRedirects(response, reverse("quiz:teacher_questions"))
        self.question_a.refresh_from_db()
        self.assertTrue(self.question_a.is_active)

    def test_cannot_activate_question_in_full_context(self):
        quiz_context = self.create_context()
        inactive = self.create_question(context=quiz_context, is_active=False)
        for _ in range(QUESTIONS_PER_CONTEXT):
            self.create_question(context=quiz_context)
        self.client.post(reverse("quiz:teacher_question_toggle", args=[inactive.pk]))
        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)

    def test_copy_and_toggle_require_post(self):
        for name in ["quiz:teacher_question_copy", "quiz:teacher_question_toggle"]:
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[self.question_a.pk]))
                self.assertEqual(response.status_code, 405)


class ContextPageTests(TeacherTestCase):
    def test_create_context_keeps_code(self):
        code = "for i in range(3):\n    print(i)"
        response = self.client.post(
            reverse("quiz:teacher_context_create"),
            {
                "title": "Цикл",
                "language": "kk",
                "text": "Программаны қараңыз.",
                "code": code.replace("\n", "\r\n"),
                "is_active": "on",
            },
        )
        quiz_context = Context.objects.get()
        self.assertRedirects(
            response, reverse("quiz:teacher_context_edit", args=[quiz_context.pk])
        )
        self.assertEqual(quiz_context.code, code)

    def test_context_page_shows_question_count(self):
        quiz_context = self.create_context()
        for _ in range(3):
            self.create_question(context=quiz_context)
        self.create_question(context=quiz_context, is_active=False)
        response = self.client.get(
            reverse("quiz:teacher_context_edit", args=[quiz_context.pk])
        )
        self.assertContains(response, "3 / 5")
        self.assertContains(self.client.get(reverse("quiz:teacher_contexts")), "3 / 5")

    def test_language_cannot_change_when_questions_linked(self):
        quiz_context = self.create_context()
        self.create_question(context=quiz_context)
        response = self.client.post(
            reverse("quiz:teacher_context_edit", args=[quiz_context.pk]),
            {"title": "Кесте", "language": "ru", "text": "Мәтін", "is_active": "on"},
        )
        self.assertIn("language", response.context["form"].errors)
        quiz_context.refresh_from_db()
        self.assertEqual(quiz_context.language, "kk")


class BankCoverageTests(TeacherTestCase):
    def test_counts_and_missing_cells(self):
        for level in ["A", "A", "B", "B", "B", "C"]:
            self.create_question(level=level)
        self.create_question(level="C", is_active=False)  # белсенді емес — саналмайды
        quiz_context = self.create_context()
        for _ in range(QUESTIONS_PER_CONTEXT):
            self.create_question(context=quiz_context)  # контекстік — бөлек саналады

        coverage = bank_coverage(informatics())
        row = next(row for row in coverage["rows"] if row["subtopic"] == self.subtopic)
        kk_cells, ru_cells = row["cells"][:4], row["cells"][4:]
        self.assertEqual([cell["count"] for cell in kk_cells], [2, 3, 1, 6])
        self.assertFalse(any(cell["missing"] for cell in kk_cells))
        self.assertEqual([cell["count"] for cell in ru_cells], [0, 0, 0, 0])
        self.assertTrue(all(cell["missing"] for cell in ru_cells))

        # Барлығы: kk бойынша A деңгейінде 2 сұрақ, квота 13 — жетіспейді
        self.assertEqual(coverage["totals"][0], {"count": 2, "missing": True, "is_total": False})
        self.assertEqual([item["count"] for item in coverage["contexts"]], [1, 0])
        self.assertTrue(coverage["contexts"][0]["missing"])

    def test_demo_bank_is_full(self):
        run_command("load_demo")
        coverage = bank_coverage(informatics())
        for row in coverage["rows"]:
            totals = [cell for cell in row["cells"] if cell["is_total"]]
            self.assertFalse(any(cell["missing"] for cell in totals), row["subtopic"])
        self.assertFalse(any(cell["missing"] for cell in coverage["totals"]))
        self.assertFalse(any(item["missing"] for item in coverage["contexts"]))

    def test_bank_page_marks_missing_in_red(self):
        response = self.client.get(reverse("quiz:teacher_bank"))
        # Бос банк: 20 тақырыпша × 8 ұяшық + қорытынды 8 ұяшық + 2 тілдің контексттері
        self.assertContains(response, 'class="missing', count=8 * SUBTOPICS_COUNT + 8 + 2)


# ---------- 3-кезең: студент кабинеті және сессиялар ----------


class StudentTestCase(TestCase):
    """Екі топ және ИНФ-21 тобындағы кірген студент."""

    def setUp(self):
        self.group = StudyGroup.objects.create(name="ИНФ-21", subject=informatics(), language="kk")
        self.other_group = StudyGroup.objects.create(name="ИНФ-22", subject=informatics(), language="kk")
        self.student = User.objects.create_user(username="student", password="pass12345")
        self.student.profile.group = self.group
        self.student.profile.save()
        self.client.force_login(self.student)

    def session_for(self, *groups, **kwargs):
        """Берілген топтарға (бос болса — барлығына) арналған сессия."""
        session = make_session(**kwargs)
        session.groups.set(groups)
        return session


class DashboardTests(StudentTestCase):
    def test_guest_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertRedirects(
            response, f"{reverse('accounts:login')}?next={reverse('quiz:dashboard')}"
        )

    def test_only_own_group_and_all_groups_sessions_are_visible(self):
        own = self.session_for(self.group, title="Өз тобы")
        for_all = self.session_for(title="Барлығына")
        both = self.session_for(self.group, self.other_group, title="Екі топқа")
        self.session_for(self.other_group, title="Бөтен топ")
        self.session_for(self.group, title="Белсенді емес", is_active=False)

        self.assertCountEqual(visible_sessions(self.student), [own, for_all, both])

        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, "Өз тобы")
        self.assertContains(response, "Барлығына")
        self.assertContains(response, "Екі топқа", count=1)
        self.assertNotContains(response, "Бөтен топ")
        self.assertNotContains(response, "Белсенді емес")

    def test_student_without_group_sees_nothing(self):
        # Студенттің пәні тобынан анықталады: тобы жоқ болса, пәні де, сессиясы да жоқ
        self.session_for(title="Барлығына")
        self.session_for(self.group, title="Өз тобы")
        self.student.profile.group = None
        self.student.profile.save()
        self.assertEqual(list(visible_sessions(self.student)), [])

    def test_open_close_boundaries(self):
        now = timezone.now()
        session = self.session_for(
            self.group, opens_at=now, closes_at=now + timedelta(hours=2)
        )
        second = timedelta(seconds=1)

        def list_name_at(moment):
            lists = dashboard_sessions(self.student, now=moment)
            for name in ["upcoming", "open", "past"]:
                if any(row["session"] == session for row in lists[name]):
                    return name
            return None

        self.assertEqual(list_name_at(session.opens_at - second), "upcoming")
        self.assertEqual(list_name_at(session.opens_at), "open")
        self.assertEqual(list_name_at(session.closes_at - second), "open")
        self.assertEqual(list_name_at(session.closes_at), "past")
        self.assertEqual(list_name_at(session.closes_at + second), "past")

    def test_three_lists_on_page(self):
        now = timezone.now()
        upcoming = self.session_for(
            self.group,
            opens_at=now + timedelta(days=2),
            closes_at=now + timedelta(days=2, hours=4),
        )
        open_now = self.session_for(self.group)
        past = self.session_for(
            self.group,
            opens_at=now - timedelta(days=2),
            closes_at=now - timedelta(days=1),
        )

        response = self.client.get(reverse("quiz:dashboard"))
        for name, session in [("upcoming", upcoming), ("open", open_now), ("past", past)]:
            with self.subTest(name=name):
                rows = response.context[name]
                self.assertEqual([row["session"] for row in rows], [session])
        # «Бастау» тек ашық сессияда
        self.assertContains(response, reverse("quiz:session_start", args=[open_now.pk]))
        self.assertNotContains(response, reverse("quiz:session_start", args=[upcoming.pk]))
        self.assertNotContains(response, reverse("quiz:session_start", args=[past.pk]))
        # Ашылуына қалған уақыт (тест кезінде бірнеше миллисекунд өтеді)
        self.assertContains(response, "Ашылуына қалды: 1 күн 23 сағ.")

    def test_split_duration(self):
        self.assertEqual(
            split_duration(timedelta(days=2, hours=3, minutes=15, seconds=59)),
            {"days": 2, "hours": 3, "minutes": 15},
        )
        self.assertEqual(
            split_duration(timedelta(minutes=45)), {"days": 0, "hours": 0, "minutes": 45}
        )

    def test_attempt_history_shows_only_own_attempts(self):
        session = self.session_for(self.group, title="Сессия-1")
        Attempt.objects.create(
            user=self.student,
            session=session,
            language="kk",
            deadline=session.closes_at,
            status=Attempt.Status.FINISHED,
            score=37,
        )
        other_student = User.objects.create_user(username="other", password="pass12345")
        other_session = self.session_for(title="Бөтен әрекет")
        Attempt.objects.create(
            user=other_student,
            session=other_session,
            language="ru",
            deadline=other_session.closes_at,
        )

        response = self.client.get(reverse("quiz:dashboard"))
        self.assertEqual(len(response.context["attempts"]), 1)
        self.assertContains(response, "37 / 50")
        self.assertContains(response, "Тапсырылды")
        # Тапсырылған сессияда «Бастау» батырмасы жоқ
        self.assertNotContains(response, reverse("quiz:session_start", args=[session.pk]))

    def test_dashboard_in_russian(self):
        self.client.cookies["django_language"] = "ru"
        self.assertContains(self.client.get(reverse("quiz:dashboard")), 'lang="ru"')


class SessionStartTests(StudentTestCase):
    def get(self, session):
        return self.client.get(reverse("quiz:session_start", args=[session.pk]))

    def test_own_session_opens(self):
        response = self.get(self.session_for(self.group))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_open"])

    def test_other_group_session_is_404(self):
        self.assertEqual(self.get(self.session_for(self.other_group)).status_code, 404)

    def test_inactive_session_is_404(self):
        self.assertEqual(self.get(self.session_for(is_active=False)).status_code, 404)

    def test_closed_session_is_not_open(self):
        now = timezone.now()
        session = self.session_for(opens_at=now - timedelta(hours=3), closes_at=now)
        self.assertFalse(self.get(session).context["is_open"])

    def test_guest_is_redirected_to_login(self):
        self.client.logout()
        response = self.get(self.session_for())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_student_cannot_open_teacher_pages(self):
        for url_name in ["teacher_questions", "teacher_contexts", "teacher_bank"]:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(f"quiz:{url_name}"))
                # staff_member_required бетті көрсетпей, admin кіру бетіне жібереді
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("admin:login"), response["Location"])


# ---------- 4-кезең: нұсқа құру ----------


class VariantTestCase(TestCase):
    """Тақырыптар мен демо банк бір рет жүктеледі (әр тілде жеткілікті сұрақ)."""

    # Әр тексеруде бірнеше кездейсоқ нұсқа құрылады
    SEEDS = range(15)

    @classmethod
    def setUpTestData(cls):
        run_command("load_topics")
        run_command("load_demo")

    def variant_questions(self, language, seed):
        """Нұсқаны құрып, сұрақтарды тест ретімен қайтарады."""
        question_ids = build_variant(informatics(), language, random.Random(seed))
        questions = Question.objects.select_related("subtopic").in_bulk(question_ids)
        return [questions[question_id] for question_id in question_ids]


class BuildVariantTests(VariantTestCase):
    def test_structure_matches_specification(self):
        single_count = SUBTOPICS_COUNT * SINGLE_QUESTIONS_PER_SUBTOPIC
        for language in ["kk", "ru"]:
            for seed in self.SEEDS:
                with self.subTest(language=language, seed=seed):
                    questions = self.variant_questions(language, seed)
                    singles = questions[:single_count]
                    context_part = questions[single_count:]

                    # 50 әртүрлі сұрақ, бәрі таңдалған тілде және белсенді
                    self.assertEqual(len(questions), QUESTIONS_TOTAL)
                    self.assertEqual(len({q.pk for q in questions}), QUESTIONS_TOTAL)
                    self.assertTrue(all(q.language == language for q in questions))
                    self.assertTrue(all(q.is_active for q in questions))

                    # 1–40: жеке сұрақтар, әр тақырыпшадан 2, тақырыпша ретімен
                    self.assertTrue(all(q.context_id is None for q in singles))
                    numbers = [q.subtopic.number for q in singles]
                    self.assertEqual(numbers, sorted(numbers))
                    self.assertEqual(
                        Counter(numbers),
                        {n: SINGLE_QUESTIONS_PER_SUBTOPIC for n in range(1, SUBTOPICS_COUNT + 1)},
                    )

                    # 41–50: 2 контекст × 5 сұрақ, әр контекстің сұрақтары қатар тұрады
                    blocks = [
                        context_part[i : i + QUESTIONS_PER_CONTEXT]
                        for i in range(0, len(context_part), QUESTIONS_PER_CONTEXT)
                    ]
                    self.assertEqual(len(blocks), CONTEXTS_PER_TEST)
                    context_ids = [block[0].context_id for block in blocks]
                    self.assertNotIn(None, context_ids)
                    self.assertEqual(len(set(context_ids)), CONTEXTS_PER_TEST)
                    for block in blocks:
                        self.assertEqual({q.context_id for q in block}, {block[0].context_id})

                    # Деңгейлер: A/B/C = 13/30/7
                    self.assertEqual(Counter(q.level for q in questions), LEVEL_QUOTA)

    def test_variants_are_random(self):
        first = {q.pk for q in self.variant_questions("kk", seed=1)}
        second = {q.pk for q in self.variant_questions("kk", seed=2)}
        self.assertNotEqual(first, second)

    def test_inactive_questions_and_contexts_are_never_chosen(self):
        # Әр тақырыпшадан бір B сұрақты және бір контекстті белсенді емес етеміз
        for subtopic in Subtopic.objects.all():
            question = subtopic.questions.filter(
                language="kk", context__isnull=True, level="B"
            ).first()
            question.is_active = False
            question.save()
        inactive_context = Context.objects.filter(language="kk").order_by("id").first()
        inactive_context.is_active = False
        inactive_context.save()

        inactive_ids = set(Question.objects.filter(is_active=False).values_list("id", flat=True))
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                questions = self.variant_questions("kk", seed)
                self.assertFalse({q.pk for q in questions} & inactive_ids)
                self.assertNotIn(inactive_context.pk, {q.context_id for q in questions})

    def test_rare_level_is_still_placed_exactly(self):
        """
        C деңгейлі жеке сұрақ тек 5 тақырыпшада қалса, квота (5) дәл толуы үшін
        сол бесеуінің әрқайсысынан C сұрақ алынуы керек (алмастыру қадамы).
        """
        singles = Question.objects.filter(language="kk", context__isnull=True, level="C")
        keep_subtopics = [1, 5, 9, 14, 20]
        singles.exclude(subtopic__number__in=keep_subtopics).update(is_active=False)
        for subtopic_number in keep_subtopics:
            extra = singles.filter(subtopic__number=subtopic_number, is_active=True)[1:]
            Question.objects.filter(pk__in=[q.pk for q in extra]).update(is_active=False)

        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                questions = self.variant_questions("kk", seed)
                self.assertEqual(Counter(q.level for q in questions), LEVEL_QUOTA)
                c_singles = [q.subtopic.number for q in questions if q.level == "C" and not q.context_id]
                self.assertEqual(sorted(c_singles), keep_subtopics)

    def test_missing_level_falls_back_and_logs_warning(self):
        # Жеке C сұрақтар мүлде жоқ: басқа деңгей алынады, журналға ескерту жазылады
        Question.objects.filter(language="kk", context__isnull=True, level="C").update(
            is_active=False
        )
        with self.assertLogs("apps.quiz.services", level="WARNING") as logs:
            questions = self.variant_questions("kk", seed=3)
        self.assertIn("квотасына дәл сәйкес емес", logs.output[0])
        self.assertEqual(len(questions), QUESTIONS_TOTAL)
        numbers = Counter(q.subtopic.number for q in questions if not q.context_id)
        self.assertEqual(set(numbers.values()), {SINGLE_QUESTIONS_PER_SUBTOPIC})

    def test_not_enough_contexts(self):
        for context in Context.objects.filter(language="ru")[1:]:
            context.is_active = False
            context.save()
        with self.assertLogs("apps.quiz.services", level="WARNING"):
            with self.assertRaises(AttemptError):
                build_variant(informatics(), "ru")

    def test_context_with_inactive_question_is_not_used(self):
        # Бір сұрағы белсенді емес контекст толық емес (4 / 5) — тестке жарамайды
        broken = Context.objects.filter(language="ru").order_by("id").first()
        broken.questions.filter(pk=broken.questions.first().pk).update(is_active=False)
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                questions = self.variant_questions("ru", seed)
                self.assertNotIn(broken.pk, {q.context_id for q in questions})

    def test_subtopic_without_questions(self):
        Question.objects.filter(
            language="kk", context__isnull=True, subtopic__number=7
        ).update(is_active=False)
        with self.assertLogs("apps.quiz.services", level="WARNING"):
            with self.assertRaises(AttemptError):
                build_variant(informatics(), "kk")


class CreateAttemptTests(VariantTestCase):
    def setUp(self):
        self.student = User.objects.create_user(username="student", password="pass12345")
        self.now = timezone.now()
        self.session = make_session(
            opens_at=self.now - timedelta(hours=1), closes_at=self.now + timedelta(hours=3)
        )

    def test_creates_50_questions_with_shuffled_answers(self):
        attempt = create_attempt(self.student, self.session, "ru", now=self.now, rng=random.Random(5))
        items = list(attempt.items.select_related("question"))
        self.assertEqual([item.order for item in items], list(range(1, QUESTIONS_TOTAL + 1)))
        self.assertEqual(attempt.language, "ru")
        self.assertEqual(attempt.status, Attempt.Status.IN_PROGRESS)
        self.assertTrue(all(item.question.language == "ru" for item in items))
        self.assertTrue(all(item.selected is None for item in items))

        sorted_orders = 0
        for item in items:
            answer_ids = list(item.question.answers.values_list("id", flat=True))
            # Нұсқалар реті — сол сұрақтың 4 жауабының алмастыруы
            self.assertEqual(sorted(item.answer_order), sorted(answer_ids))
            self.assertEqual(len(item.answer_order), ANSWERS_PER_QUESTION)
            sorted_orders += item.answer_order == sorted(answer_ids)
        # Реті араластырылған (50 сұрақтың бәрі өз ретімен қалуы мүмкін емес)
        self.assertLess(sorted_orders, QUESTIONS_TOTAL // 2)

    def test_deadline_is_125_minutes(self):
        attempt = create_attempt(self.student, self.session, "kk", now=self.now)
        self.assertEqual(attempt.started_at, self.now)
        self.assertEqual(attempt.deadline, self.now + timedelta(minutes=INFORMATICS_MINUTES))

    def test_deadline_does_not_exceed_session_close(self):
        self.session.closes_at = self.now + timedelta(minutes=30)
        self.session.save()
        attempt = create_attempt(self.student, self.session, "kk", now=self.now)
        self.assertEqual(attempt.deadline, self.session.closes_at)

    def test_session_must_be_open(self):
        moments = [
            self.session.opens_at - timedelta(seconds=1),  # әлі ашылмаған
            self.session.closes_at,  # жабылды
            self.session.closes_at + timedelta(minutes=1),
        ]
        for moment in moments:
            with self.subTest(moment=moment):
                with self.assertRaises(AttemptError):
                    create_attempt(self.student, self.session, "kk", now=moment)
        # Ашылған сәтте бастауға болады
        create_attempt(self.student, self.session, "kk", now=self.session.opens_at)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_inactive_session(self):
        self.session.is_active = False
        self.session.save()
        with self.assertRaises(AttemptError):
            create_attempt(self.student, self.session, "kk", now=self.now)

    def test_only_one_attempt_per_session(self):
        create_attempt(self.student, self.session, "kk", now=self.now)
        with self.assertRaises(AttemptError):
            create_attempt(self.student, self.session, "ru", now=self.now)
        self.assertEqual(Attempt.objects.count(), 1)
        self.assertEqual(AttemptQuestion.objects.count(), QUESTIONS_TOTAL)

    def test_nothing_saved_when_bank_is_incomplete(self):
        Context.objects.filter(language="kk").update(is_active=False)
        with self.assertLogs("apps.quiz.services", level="WARNING"):
            with self.assertRaises(AttemptError):
                create_attempt(self.student, self.session, "kk", now=self.now)
        self.assertFalse(Attempt.objects.exists())


class BankSampleTests(VariantTestCase):
    """Оқытушының «Үлгі нұсқа» беті: /teacher/bank/sample/?lang=..."""

    def setUp(self):
        self.teacher = make_teacher()
        self.client.force_login(self.teacher)
        self.url = reverse("quiz:teacher_bank_sample")

    def get(self, lang=None):
        return self.client.get(self.url, {"lang": lang} if lang else {})

    def test_sample_for_each_language(self):
        for language in ["kk", "ru"]:
            with self.subTest(language=language):
                response = self.get(language)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["language"], language)
                summary = response.context["summary"]
                self.assertEqual(len(summary["questions"]), QUESTIONS_TOTAL)
                self.assertTrue(all(q.language == language for q in summary["questions"]))
                self.assertTrue(summary["all_ok"])
                self.assertEqual(
                    {item["level"]: item["count"] for item in summary["levels"]}, LEVEL_QUOTA
                )
                self.assertEqual(len(summary["subtopics"]), SUBTOPICS_COUNT)
                self.assertEqual(len(summary["contexts"]), CONTEXTS_PER_TEST)
                # 50 жолдық кесте: әр сұраққа бір жол (тақырып(ша) атауы жолда)
                self.assertContains(response, "<tr", count=QUESTIONS_TOTAL + 1)
                self.assertContains(response, "✓")

    def test_nothing_is_saved(self):
        self.get("kk")
        self.get("ru")
        self.assertFalse(Attempt.objects.exists())
        self.assertFalse(AttemptQuestion.objects.exists())

    def test_new_variant_on_every_reload(self):
        first = [q.pk for q in self.get("kk").context["summary"]["questions"]]
        second = [q.pk for q in self.get("kk").context["summary"]["questions"]]
        self.assertNotEqual(first, second)
        self.assertIn("no-cache", self.get("kk")["Cache-Control"])

    def test_unknown_or_missing_language_falls_back_to_kazakh(self):
        for lang in [None, "en", "x"]:
            with self.subTest(lang=lang):
                self.assertEqual(self.get(lang).context["language"], "kk")

    def test_bank_error_is_shown(self):
        Context.objects.filter(language="ru").update(is_active=False)
        with self.assertLogs("apps.quiz.services", level="WARNING"):
            response = self.get("ru")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("summary", response.context)
        self.assertContains(response, "Нұсқа құру мүмкін болмады")
        self.assertContains(response, "Банкте «Орысша» тіліндегі сұрақтар жеткіліксіз")

    def test_rule_violation_is_marked(self):
        # Жеке C сұрақтар жоқ: нұсқа құрылады, бірақ квота орындалмайды (✗)
        Question.objects.filter(language="kk", context__isnull=True, level="C").update(
            is_active=False
        )
        with self.assertLogs("apps.quiz.services", level="WARNING"):
            response = self.get("kk")
        summary = response.context["summary"]
        self.assertFalse(summary["all_ok"])
        self.assertFalse(summary["checks"]["levels"])
        self.assertTrue(summary["checks"]["subtopics"])
        self.assertTrue(summary["checks"]["contexts"])
        self.assertContains(response, "✗")

    def test_bank_page_has_sample_buttons(self):
        response = self.client.get(reverse("quiz:teacher_bank"))
        self.assertContains(response, f"{self.url}?lang=kk")
        self.assertContains(response, f"{self.url}?lang=ru")

    def test_student_cannot_open(self):
        student = User.objects.create_user(username="student", password="pass12345")
        self.client.force_login(student)
        response = self.get("kk")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_page_in_russian_interface(self):
        self.client.cookies["django_language"] = "ru"
        self.assertContains(self.get("kk"), "Образец варианта")


# ---------- Екі тіл ----------


class TranslationTests(StudentTestCase):
    """Тіл ауыстырғаннан кейін бет мәтіні орысшаға, қайта қазақшаға ауысады."""

    def switch_language(self, code):
        self.client.post(reverse("set_language"), {"language": code, "next": "/"})

    def test_dashboard_switches_language(self):
        self.switch_language("ru")
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, "Личный кабинет")
        self.assertContains(response, "Выйти")
        self.assertNotContains(response, "Жеке кабинет")

        self.switch_language("kk")
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, "Жеке кабинет")
        self.assertNotContains(response, "Личный кабинет")

    def test_pages_are_in_russian(self):
        self.client.logout()
        self.switch_language("ru")
        pages = {
            reverse("quiz:home"): "Подготовка к тесту ОЗП",
            reverse("accounts:login"): "ИИН или логин",
        }
        for url, text in pages.items():
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), text)

        response = self.client.post(
            reverse("accounts:login"), {"username": "student", "password": "wrong"}
        )
        self.assertContains(response, "Неверный ИИН (логин) или код (пароль).")

    def test_teacher_pages_are_in_russian(self):
        run_command("load_topics")
        teacher = make_teacher()
        self.client.force_login(teacher)
        self.switch_language("ru")
        pages = {
            reverse("quiz:teacher_questions"): "Новый вопрос",
            reverse("quiz:teacher_question_create"): "Сохранить и добавить следующий",
            reverse("quiz:teacher_contexts"): "Новый контекст",
            reverse("quiz:teacher_bank"): "Наполненность банка",
        }
        for url, text in pages.items():
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), text)

    def test_russian_catalog_is_complete(self):
        """Орысша .po файлында аударылмаған жол қалмауы керек."""
        po_path = settings.BASE_DIR / "locale" / "ru" / "LC_MESSAGES" / "django.po"
        entries = po_path.read_text(encoding="utf-8").split("\n\n")[1:]
        # Аударылмаған жазба `msgstr ""` жолымен аяқталады (жалғасы жоқ)
        untranslated = [entry for entry in entries if entry.strip().endswith('msgstr ""')]
        self.assertEqual(untranslated, [])
        # makemessages ұқсас жолдан «болжап» қойған (fuzzy) аудармалар компиляцияланбайды
        fuzzy = [entry for entry in entries if "fuzzy" in entry]
        self.assertEqual(fuzzy, [])


# ---------- 5-кезең: тест тапсыру, таймер, нәтиже ----------


class TakeTestCase(VariantTestCase):
    """Демо банк, ашық сессия және кірген студент."""

    def setUp(self):
        self.student = make_student(first_name="Асқар")
        self.client.force_login(self.student)
        self.session = make_session(title="Күзгі сынақ")

    def start(self, language="kk"):
        """«Бастау» батырмасы: POST /session/<id>/start/."""
        return self.client.post(
            reverse("quiz:session_start", args=[self.session.pk]), {"language": language}
        )

    def new_attempt(self, user=None, session=None):
        return create_attempt(user or self.student, session or self.session, "kk")

    def question_url(self, attempt, number):
        return reverse("quiz:attempt_question", args=[attempt.pk, number])

    def answer(self, attempt, number, answer_id):
        return self.client.post(self.question_url(attempt, number), {"answer": answer_id})

    def correct_answer(self, item):
        return item.question.answers.get(is_correct=True)

    def wrong_answer(self, item):
        return item.question.answers.filter(is_correct=False).first()

    def expire(self, attempt):
        """Мерзімді өткізіп жібереді (тест әлі аяқталмаған)."""
        attempt.deadline = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["deadline"])


class StartTestPageTests(TakeTestCase):
    def set_group_language(self, language):
        group = self.student.profile.group
        group.language = language
        group.save()

    def test_start_page_shows_rules_and_group_language(self):
        response = self.client.get(reverse("quiz:session_start", args=[self.session.pk]))
        # Тілді таңдау жоқ — тобының тілі көрсетіледі
        self.assertNotContains(response, 'type="radio"')
        self.assertContains(response, "Тест тілі: <strong>Қазақша</strong>")
        self.assertContains(response, f"Сұрақтар саны: {QUESTIONS_TOTAL}")
        self.assertContains(response, f"{INFORMATICS_MINUTES} минут")
        self.assertContains(response, timezone.localtime(self.session.closes_at).strftime("%d.%m.%Y, %H:%M"))

    def test_start_uses_group_language(self):
        self.set_group_language("ru")
        response = self.start()
        attempt = Attempt.objects.get(user=self.student, session=self.session)
        self.assertRedirects(response, self.question_url(attempt, 1))
        self.assertEqual(attempt.language, "ru")
        self.assertEqual(attempt.items.count(), QUESTIONS_TOTAL)
        self.assertFalse(attempt.items.exclude(question__language="ru").exists())

    def test_second_start_does_not_create_new_attempt(self):
        self.start()
        attempt = Attempt.objects.get(user=self.student)
        item = attempt.items.get(order=1)
        self.answer(attempt, 1, self.correct_answer(item).pk)

        # Қайта «Бастау»: жаңа әрекет жоқ, жауап берілмеген бірінші сұраққа қайтады
        for response in [
            self.start("ru"),
            self.client.get(reverse("quiz:session_start", args=[self.session.pk])),
        ]:
            self.assertRedirects(response, self.question_url(attempt, 2))
        self.assertEqual(Attempt.objects.filter(user=self.student).count(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.language, "kk")

    def test_finished_attempt_redirects_to_result(self):
        attempt = self.new_attempt()
        finish_attempt(attempt)
        response = self.start()
        self.assertRedirects(response, reverse("quiz:attempt_result", args=[attempt.pk]))
        self.assertEqual(Attempt.objects.count(), 1)

    def test_closed_session_cannot_be_started(self):
        now = timezone.now()
        self.session.opens_at = now - timedelta(hours=3)
        self.session.closes_at = now - timedelta(minutes=1)
        self.session.save()
        response = self.start()
        self.assertRedirects(response, reverse("quiz:session_start", args=[self.session.pk]))
        self.assertFalse(Attempt.objects.exists())
        response = self.client.get(response["Location"])
        self.assertContains(response, "Бұл сессия қазір ашық емес.")
        self.assertNotContains(response, 'id="id_language_0"')

    def test_posted_language_is_ignored(self):
        # Студент формаға басқа тілді жіберсе де, тест тобының тілінде өтеді
        self.start("ru")
        attempt = Attempt.objects.get(user=self.student)
        self.assertEqual(attempt.language, "kk")
        self.assertFalse(attempt.items.exclude(question__language="kk").exists())

    def test_bank_error_is_shown(self):
        self.set_group_language("ru")
        Question.objects.filter(language="ru").update(is_active=False)
        response = self.start()
        self.assertFalse(Attempt.objects.exists())
        self.assertContains(self.client.get(response["Location"]), "жеткіліксіз")


class QuestionPageTests(TakeTestCase):
    def setUp(self):
        super().setUp()
        self.attempt = self.new_attempt()

    def test_question_page_shows_question_navigation_and_timer(self):
        item = self.attempt.items.get(order=41)
        response = self.client.get(self.question_url(self.attempt, 41))
        self.assertEqual(response.status_code, 200)
        # Контекст мәтіні 41-сұрақ блогында, сұрақтың үстінде
        content = response.content.decode()
        block_start = content.index('id="question-41"')
        title_at = content.index(escape(item.question.context.title), block_start)
        self.assertLess(title_at, content.index('name="answer"', block_start))
        # 1–50 навигация, алдыңғы/келесі
        for number in [1, 40, 42, 50]:
            self.assertContains(response, f'href="{self.question_url(self.attempt, number)}"')
        self.assertContains(response, 'aria-current="page"', count=1)
        # Таймер: қалған уақыт сервердегі мерзімнен
        remaining = response.context["remaining_seconds"]
        self.assertTrue(INFORMATICS_MINUTES * 60 - 5 <= remaining <= INFORMATICS_MINUTES * 60)
        self.assertContains(response, f'data-remaining="{remaining}"')
        self.assertContains(response, reverse("quiz:attempt_finish", args=[self.attempt.pk]))
        self.assertContains(response, "js/timer.js")

    def test_answers_are_shown_in_shuffled_order(self):
        item = self.attempt.items.get(order=1)
        response = self.client.get(self.question_url(self.attempt, 1))
        answers = response.context["questions"][0]["answers"]
        self.assertEqual([answer["id"] for answer in answers], item.answer_order)
        self.assertEqual([answer["letter"] for answer in answers], ["A", "B", "C", "D"])

    def test_code_indentation_is_kept_in_pre(self):
        item = self.attempt.items.get(order=7)
        code = "for i in range(3):\n    if i > 0:\n        print(i)"
        Question.objects.filter(pk=item.question_id).update(code=code)
        response = self.client.get(self.question_url(self.attempt, 7))
        self.assertContains(response, f'<pre class="code-block">{escape(code)}</pre>')

    def test_correct_answer_is_not_in_html(self):
        for number in [1, 25, 45]:
            with self.subTest(number=number):
                response = self.client.get(self.question_url(self.attempt, number))
                self.assertNotContains(response, "is_correct")
                self.assertNotContains(response, "list-group-item-success")
                self.assertNotContains(response, "дұрыс</span>")
                # Шаблонға (барлық 50 сұрақтың) тек мәтін, сурет пен id жетеді
                for row in response.context["questions"]:
                    for answer in row["answers"]:
                        self.assertEqual(set(answer), {"letter", "id", "text", "image"})
        # Аяқтау беті де дұрыс жауапты көрсетпейді
        response = self.client.get(reverse("quiz:attempt_finish", args=[self.attempt.pk]))
        self.assertNotContains(response, "list-group-item-success")

    def test_answer_is_saved_and_can_be_changed(self):
        item = self.attempt.items.get(order=3)
        wrong, correct = self.wrong_answer(item), self.correct_answer(item)

        response = self.answer(self.attempt, 3, wrong.pk)
        self.assertRedirects(response, self.question_url(self.attempt, 3))
        item.refresh_from_db()
        self.assertEqual(item.selected, wrong)

        self.answer(self.attempt, 3, correct.pk)
        item.refresh_from_db()
        self.assertEqual(item.selected, correct)

        # Бет қайта ашылса, таңдау сақталған, навигацияда боялған
        response = self.client.get(self.question_url(self.attempt, 3))
        self.assertRegex(
            response.content.decode(),
            rf'value="{correct.pk}"\s+onchange="[^"]*"\s+checked',
        )
        answered = [cell["number"] for cell in response.context["navigation"] if cell["answered"]]
        self.assertEqual(answered, [3])

    def test_answer_from_other_question_is_rejected(self):
        other_item = self.attempt.items.get(order=2)
        response = self.answer(self.attempt, 1, self.correct_answer(other_item).pk)
        self.assertRedirects(response, self.question_url(self.attempt, 1))
        self.assertIsNone(self.attempt.items.get(order=1).selected)
        for value in ["", "abc"]:
            self.answer(self.attempt, 1, value)
        self.assertIsNone(self.attempt.items.get(order=1).selected)

    def test_unknown_question_number_is_404(self):
        for number in [0, QUESTIONS_TOTAL + 1]:
            with self.subTest(number=number):
                response = self.client.get(self.question_url(self.attempt, number))
                self.assertEqual(response.status_code, 404)

    def test_finished_attempt_answers_cannot_change(self):
        item = self.attempt.items.get(order=1)
        finish_attempt(self.attempt)
        response = self.answer(self.attempt, 1, self.correct_answer(item).pk)
        self.assertRedirects(response, reverse("quiz:attempt_result", args=[self.attempt.pk]))
        item.refresh_from_db()
        self.assertIsNone(item.selected)


class DeadlineTests(TakeTestCase):
    def setUp(self):
        super().setUp()
        self.attempt = self.new_attempt()
        self.item = self.attempt.items.get(order=1)

    def test_answer_after_deadline_is_rejected(self):
        self.expire(self.attempt)
        response = self.answer(self.attempt, 1, self.correct_answer(self.item).pk)
        self.assertRedirects(response, reverse("quiz:attempt_result", args=[self.attempt.pk]))
        self.item.refresh_from_db()
        self.assertIsNone(self.item.selected)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.FINISHED)
        self.assertEqual(self.attempt.score, 0)

    def test_save_answer_checks_deadline_on_server(self):
        answer = self.correct_answer(self.item)
        second = timedelta(seconds=1)
        self.assertTrue(save_answer(self.item, answer.pk, now=self.attempt.deadline - second))
        self.assertFalse(save_answer(self.item, answer.pk, now=self.attempt.deadline))
        self.assertFalse(save_answer(self.item, answer.pk, now=self.attempt.deadline + second))

    def test_expired_attempt_is_finished_when_opened(self):
        self.item.selected = self.correct_answer(self.item)
        self.item.save()
        result_url = reverse("quiz:attempt_result", args=[self.attempt.pk])
        urls = [
            self.question_url(self.attempt, 5),
            reverse("quiz:attempt_finish", args=[self.attempt.pk]),
            reverse("quiz:session_start", args=[self.session.pk]),
            result_url,
        ]
        for url in urls:
            with self.subTest(url=url):
                Attempt.objects.filter(pk=self.attempt.pk).update(
                    status=Attempt.Status.IN_PROGRESS, score=None, finished_at=None
                )
                self.expire(self.attempt)
                response = self.client.get(url)
                if url != result_url:
                    self.assertRedirects(response, result_url)
                self.attempt.refresh_from_db()
                self.assertEqual(self.attempt.status, Attempt.Status.FINISHED)
                self.assertEqual(self.attempt.score, 1)
                # Аяқталу уақыты — мерзім, кейін ашылған уақыт емес
                self.assertEqual(self.attempt.finished_at, self.attempt.deadline)

    def test_dashboard_finishes_expired_attempts(self):
        self.expire(self.attempt)
        response = self.client.get(reverse("quiz:dashboard"))
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.FINISHED)
        self.assertContains(response, f"0 / {QUESTIONS_TOTAL}")
        self.assertContains(response, reverse("quiz:attempt_result", args=[self.attempt.pk]))

    def test_teacher_sees_expired_attempt_as_finished(self):
        self.expire(self.attempt)
        teacher = make_teacher()
        self.client.force_login(teacher)
        response = self.client.get(reverse("quiz:attempt_result", args=[self.attempt.pk]))
        self.assertContains(response, f"0 / {QUESTIONS_TOTAL}")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.FINISHED)

    def test_finish_expired_command(self):
        other = User.objects.create_user(username="other", password="pass12345")
        running = self.new_attempt(user=other)
        self.expire(self.attempt)

        out = StringIO()
        call_command("finish_expired", stdout=out)
        self.assertIn("Аяқталған әрекеттер: 1", out.getvalue())
        self.attempt.refresh_from_db()
        running.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.FINISHED)
        self.assertEqual(running.status, Attempt.Status.IN_PROGRESS)
        self.assertEqual(finish_expired_attempts(), 0)

    def test_timer_never_shows_negative(self):
        self.assertEqual(
            remaining_seconds(self.attempt, now=self.attempt.deadline + timedelta(minutes=1)), 0
        )
        self.assertEqual(
            remaining_seconds(self.attempt, now=self.attempt.deadline - timedelta(seconds=1.5)), 2
        )


class FinishAndResultTests(TakeTestCase):
    def test_full_path_start_answer_finish_result(self):
        # Бастау
        response = self.start("kk")
        attempt = Attempt.objects.get(user=self.student)
        self.assertRedirects(response, self.question_url(attempt, 1))

        # Жауаптар: 1–3 дұрыс, 4 қате, қалғаны бос
        for number in [1, 2, 3]:
            item = attempt.items.get(order=number)
            self.answer(attempt, number, self.correct_answer(item).pk)
        self.answer(attempt, 4, self.wrong_answer(attempt.items.get(order=4)).pk)

        # Аяқтау: растау беті жауап берілмегендер санын көрсетеді
        finish_url = reverse("quiz:attempt_finish", args=[attempt.pk])
        response = self.client.get(finish_url)
        self.assertContains(response, f"Жауап берілмеген сұрақтар саны: <strong>{QUESTIONS_TOTAL - 4}</strong>")
        self.assertEqual(response.context["unanswered"], list(range(5, QUESTIONS_TOTAL + 1)))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.IN_PROGRESS)

        response = self.client.post(finish_url)
        result_url = reverse("quiz:attempt_result", args=[attempt.pk])
        self.assertRedirects(response, result_url)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.FINISHED)
        self.assertEqual(attempt.score, 3)
        self.assertIsNotNone(attempt.finished_at)

        # Нәтиже
        response = self.client.get(result_url)
        self.assertContains(response, f"3 / {QUESTIONS_TOTAL}")
        self.assertContains(response, "6%")
        result = response.context["result"]
        self.assertEqual(result["score"], 3)
        self.assertEqual(sum(row["total"] for row in result["levels"]), QUESTIONS_TOTAL)
        self.assertEqual(
            [row["total"] for row in result["levels"]], [LEVEL_QUOTA[level] for level in "ABC"]
        )
        self.assertEqual(sum(row["correct"] for row in result["levels"]), 3)
        self.assertEqual(len(result["topics"]), 11)
        self.assertEqual(sum(row["total"] for row in result["topics"]), QUESTIONS_TOTAL)
        self.assertEqual(sum(row["correct"] for row in result["topics"]), 3)
        self.assertEqual(len(result["questions"]), QUESTIONS_TOTAL)

        # Аяқталған тестке қайта кірсе — нәтиже беті; жауап өзгермейді
        self.assertRedirects(self.client.get(self.question_url(attempt, 1)), result_url)
        self.assertRedirects(self.client.post(finish_url), result_url)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 3)

        # Кабинетте балы көрінеді
        self.assertContains(self.client.get(reverse("quiz:dashboard")), f"3 / {QUESTIONS_TOTAL}")

    def test_spent_time(self):
        attempt = self.new_attempt()
        finish_attempt(attempt, now=attempt.started_at + timedelta(minutes=42, seconds=7))
        response = self.client.get(reverse("quiz:attempt_result", args=[attempt.pk]))
        self.assertContains(response, "42 мин 7 с")

    def test_finish_all_answered(self):
        attempt = self.new_attempt()
        for item in attempt.items.all():
            item.selected = self.correct_answer(item)
            item.save()
        response = self.client.get(reverse("quiz:attempt_finish", args=[attempt.pk]))
        self.assertContains(response, "Барлық сұраққа жауап бердіңіз.")
        self.assertEqual(finish_attempt(attempt).score, QUESTIONS_TOTAL)

    def test_unfinished_result_redirects_student_to_test(self):
        attempt = self.new_attempt()
        response = self.client.get(reverse("quiz:attempt_result", args=[attempt.pk]))
        self.assertRedirects(response, self.question_url(attempt, 1))

    def test_result_page_in_russian(self):
        attempt = self.new_attempt()
        finish_attempt(attempt)
        self.client.cookies["django_language"] = "ru"
        response = self.client.get(reverse("quiz:attempt_result", args=[attempt.pk]))
        self.assertContains(response, "Результат теста")
        self.assertNotContains(response, "Тест нәтижесі")


class ShowAnswersTests(TakeTestCase):
    def setUp(self):
        super().setUp()
        self.attempt = self.new_attempt()
        self.item = self.attempt.items.get(order=1)
        self.wrong = self.wrong_answer(self.item)
        self.item.selected = self.wrong
        self.item.save()
        self.attempt = finish_attempt(self.attempt)
        self.result_url = reverse("quiz:attempt_result", args=[self.attempt.pk])

    def assert_answers_shown(self, response):
        self.assertTrue(response.context["show_answers"])
        self.assertContains(response, "таңдалған жауап", count=1)
        self.assertContains(response, "list-group-item-success", count=QUESTIONS_TOTAL)
        self.assertContains(response, "list-group-item-danger", count=1)

    def assert_answers_hidden(self, response):
        self.assertFalse(response.context["show_answers"])
        self.assertEqual(response.context["result"]["questions"], [])
        self.assertNotContains(response, "list-group-item-success")
        self.assertNotContains(response, "таңдалған жауап")
        self.assertContains(response, "Дұрыс жауаптар сессия жабылғаннан кейін")

    def test_after_finish(self):
        self.assert_answers_shown(self.client.get(self.result_url))

    def test_after_close(self):
        self.session.show_answers = ExamSession.ShowAnswers.AFTER_CLOSE
        self.session.save()
        response = self.client.get(self.result_url)
        self.assert_answers_hidden(response)
        # Балл мен талдау бәрібір көрінеді
        self.assertContains(response, f"0 / {QUESTIONS_TOTAL}")

        # Сессия жабылғаннан кейін — көрінеді
        self.session.closes_at = timezone.now() - timedelta(seconds=1)
        self.session.save()
        self.assert_answers_shown(self.client.get(self.result_url))

    def test_teacher_always_sees_answers(self):
        self.session.show_answers = ExamSession.ShowAnswers.AFTER_CLOSE
        self.session.save()
        teacher = make_teacher()
        self.client.force_login(teacher)
        response = self.client.get(self.result_url)
        self.assert_answers_shown(response)
        self.assertContains(response, "Асқар")

    def test_can_see_answers_rules(self):
        self.attempt.session = self.session
        self.session.show_answers = ExamSession.ShowAnswers.AFTER_CLOSE
        closes = self.session.closes_at
        self.assertFalse(can_see_answers(self.attempt, self.student, now=closes - timedelta(seconds=1)))
        self.assertTrue(can_see_answers(self.attempt, self.student, now=closes))
        # Аяқталмаған тестте — ешқашан
        self.session.show_answers = ExamSession.ShowAnswers.AFTER_FINISH
        running = Attempt(session=self.session, status=Attempt.Status.IN_PROGRESS)
        self.assertFalse(can_see_answers(running, self.student))


class AttemptAccessTests(TakeTestCase):
    def setUp(self):
        super().setUp()
        other = User.objects.create_user(username="other", password="pass12345")
        self.foreign = self.new_attempt(user=other)

    def test_foreign_attempt_is_404(self):
        urls = [
            reverse("quiz:attempt_question", args=[self.foreign.pk, 1]),
            reverse("quiz:attempt_finish", args=[self.foreign.pk]),
            reverse("quiz:attempt_result", args=[self.foreign.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, {"answer": 1}).status_code, 404)
        # Бөтен тест өзгермеді
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.status, Attempt.Status.IN_PROGRESS)
        self.assertFalse(self.foreign.items.filter(selected__isnull=False).exists())

        finish_attempt(self.foreign)
        url = reverse("quiz:attempt_result", args=[self.foreign.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_guest_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("quiz:attempt_result", args=[self.foreign.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_teacher_can_see_any_result(self):
        teacher = make_teacher()
        self.client.force_login(teacher)
        url = reverse("quiz:attempt_result", args=[self.foreign.pk])
        # Аяқталмаған тест — тек хабарлама
        response = self.client.get(url)
        self.assertContains(response, "Тест әлі аяқталмаған")
        self.assertNotContains(response, "list-group-item-success")

        finish_attempt(self.foreign)
        self.assertContains(self.client.get(url), f"0 / {QUESTIONS_TOTAL}")
        # Бірақ бөтен тестке жауап бере алмайды
        question_url = reverse("quiz:attempt_question", args=[self.foreign.pk, 1])
        self.assertEqual(self.client.get(question_url).status_code, 404)


# ---------- 6-кезең: оқытушының нәтижелер беті және CSV ----------


class TeacherResultsTests(TestCase):
    """Екі топ, екі сессия және бірнеше әрекет (сұрақсыз — тек балы)."""

    def setUp(self):
        self.teacher = make_teacher()
        self.client.force_login(self.teacher)
        self.group_a = StudyGroup.objects.create(name="ИНФ-21", subject=informatics(), language="kk")
        self.group_b = StudyGroup.objects.create(name="ИНФ-22", subject=informatics(), language="kk")
        self.autumn = make_session(title="Күзгі сессия")
        self.spring = make_session(title="Көктемгі сессия")
        self.aigerim = self.make_student("aigerim", "Айгерім", "Сапарова", self.group_a)
        self.dana = self.make_student("dana", "Дана", "Әлиева", self.group_b)

        self.finished = self.make_attempt(self.aigerim, self.autumn, score=37)
        self.other_session = self.make_attempt(self.aigerim, self.spring, score=20)
        self.running = self.make_attempt(self.dana, self.autumn, score=None)

    def make_student(self, username, first_name, last_name, group):
        user = User.objects.create_user(
            username=username, password="pass12345", first_name=first_name, last_name=last_name
        )
        user.profile.group = group
        user.profile.save()
        return user

    def make_attempt(self, user, session, score):
        now = timezone.now()
        data = {
            "user": user,
            "session": session,
            "language": "kk",
            "started_at": now - timedelta(minutes=50),
            "deadline": now + timedelta(minutes=75),
        }
        if score is not None:
            data.update(
                status=Attempt.Status.FINISHED, score=score, finished_at=now - timedelta(minutes=8)
            )
        return Attempt.objects.create(**data)

    def get(self, **params):
        return self.client.get(reverse("quiz:teacher_results"), params)

    def export(self, **params):
        response = self.client.get(reverse("quiz:teacher_results_export"), params)
        return response, response.content.decode("utf-8")

    def test_only_teacher_can_open(self):
        student = User.objects.create_user(username="student", password="pass12345")
        self.client.force_login(student)
        for url_name in ["teacher_results", "teacher_results_export"]:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(f"quiz:{url_name}"))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("admin:login"), response["Location"])

    def test_all_results_are_listed(self):
        response = self.get()
        self.assertEqual(len(response.context["rows"]), 3)
        self.assertContains(response, "Айгерім Сапарова")
        self.assertContains(response, "ИНФ-21")
        self.assertContains(response, f"37 / {QUESTIONS_TOTAL}")
        self.assertContains(response, "74%")
        self.assertContains(response, "Жүріп жатыр")
        self.assertContains(response, reverse("quiz:attempt_result", args=[self.finished.pk]))
        self.assertEqual(
            response.context["summary"], {"count": 3, "finished": 2, "average": 28.5}
        )

    def test_filter_by_session_and_group(self):
        response = self.get(session=self.autumn.pk)
        attempts = [row["attempt"] for row in response.context["rows"]]
        self.assertCountEqual(attempts, [self.finished, self.running])

        response = self.get(group=self.group_a.pk)
        attempts = [row["attempt"] for row in response.context["rows"]]
        self.assertCountEqual(attempts, [self.finished, self.other_session])

        response = self.get(session=self.autumn.pk, group=self.group_b.pk)
        self.assertEqual([row["attempt"] for row in response.context["rows"]], [self.running])
        # CSV сілтемесі сүзгіні сақтайды
        self.assertContains(
            response,
            f"{reverse('quiz:teacher_results_export')}?session={self.autumn.pk}&amp;group={self.group_b.pk}",
        )

    def test_expired_attempt_is_shown_finished(self):
        Attempt.objects.filter(pk=self.running.pk).update(
            deadline=timezone.now() - timedelta(minutes=1)
        )
        self.get()
        self.running.refresh_from_db()
        self.assertEqual(self.running.status, Attempt.Status.FINISHED)
        self.assertEqual(self.running.score, 0)

    def test_csv_export(self):
        response, content = self.export()
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertTrue(content.startswith("﻿"))

        lines = content.lstrip("﻿").splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("Студент;Логин;Топ;Пән;Сессия"))
        row = next(line for line in lines if "Күзгі сессия" in line and "aigerim" in line)
        columns = row.split(";")
        self.assertEqual(
            columns[:5], ["Айгерім Сапарова", "aigerim", "ИНФ-21", "Информатика", "Күзгі сессия"]
        )
        self.assertEqual(columns[8:], ["Аяқталды", "37", "74", "42"])
        # Аяқталмаған тестте балл жоқ
        running = next(line for line in lines if "dana" in line).split(";")
        self.assertEqual(running[8:], ["Жүріп жатыр", "", "", ""])

    def test_csv_export_escapes_formulas(self):
        # Студент атын өзі енгізеді: Excel оны формула деп орындамауы керек
        User.objects.filter(pk=self.aigerim.pk).update(
            first_name='=HYPERLINK("http://evil")', last_name="", username="+aigerim"
        )
        _response, content = self.export(group=self.group_a.pk)
        row = content.lstrip("﻿").splitlines()[1].split(";")
        self.assertEqual(row[0], "\"'=HYPERLINK(\"\"http://evil\"\")\"")
        self.assertEqual(row[1], "'+aigerim")

    def test_csv_safe(self):
        self.assertEqual(csv_safe("Айгерім"), "Айгерім")
        self.assertEqual(csv_safe(""), "")
        for value in ["=1+1", "+7", "-2", "@SUM(A1)", chr(9) + "x", chr(13) + "x"]:
            with self.subTest(value=value):
                self.assertEqual(csv_safe(value), "'" + value)

    def test_csv_export_uses_filter(self):
        _response, content = self.export(group=self.group_b.pk)
        lines = content.lstrip("﻿").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("dana", lines[1])

    def test_results_page_in_russian(self):
        self.client.cookies["django_language"] = "ru"
        self.assertContains(self.get(), "Скачать CSV")
        _response, content = self.export()
        self.assertTrue(content.lstrip("﻿").startswith("Студент;Логин;Группа;Предмет;Сессия"))

    def test_teacher_navigation_has_results_link(self):
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertContains(response, reverse("quiz:teacher_results"))


# ---------- 7-кезең: барлық беттің орысша аудармасы ----------

# Тек қазақ әліпбиінде бар әріптер: орысша бетте болмауы керек
KAZAKH_ONLY_LETTERS = re.compile(r"[әғқңөұүһіӘҒҚҢӨҰҮҺІ]")


def visible_text(response):
    """Беттің мәтіні: <style>, <script> және HTML түсініктемелерінсіз."""
    html = response.content.decode()
    html = re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=re.S)
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


class FullTranslationTests(VariantTestCase):
    """
    Барлық бет орысша ашылғанда қазақша мәтін қалмауы керек (аударылмаған
    жол табылса, тест оның айналасын көрсетеді). Деректер орысша/латынша.
    """

    def setUp(self):
        # Беттерде қазақша сұрақ/контекст мазмұны шықпауы үшін — тек орысша банк
        Question.objects.filter(language="kk").delete()
        Context.objects.filter(language="kk").delete()

        self.group = StudyGroup.objects.create(name="INF-21", subject=informatics(), language="kk")
        self.student = User.objects.create_user(
            username="student", password="pass12345", first_name="Ivan", last_name="Petrov"
        )
        self.student.profile.group = self.group
        self.student.profile.save()
        self.teacher = make_teacher()
        self.session = make_session(title="Test session")
        self.attempt = create_attempt(self.student, self.session, "ru")
        self.client.cookies["django_language"] = "ru"

    def assert_russian(self, response, name):
        self.assertIn(response.status_code, [200])
        text = visible_text(response)
        found = [
            text[max(0, match.start() - 60) : match.end() + 60]
            for match in KAZAKH_ONLY_LETTERS.finditer(text)
        ]
        self.assertEqual(found, [], f"{name}: аударылмаған мәтін")

    def check_pages(self, pages):
        for name, (method, url, data) in pages.items():
            with self.subTest(page=name):
                if method == "post":
                    response = self.client.post(url, data)
                else:
                    response = self.client.get(url, data)
                self.assert_russian(response, name)

    def test_guest_pages(self):
        self.check_pages(
            {
                "home": ("get", reverse("quiz:home"), {}),
                "login": ("get", reverse("accounts:login"), {}),
                "login error": (
                    "post",
                    reverse("accounts:login"),
                    {"username": "student", "password": "wrong"},
                ),
            }
        )

    def test_student_pages(self):
        self.client.force_login(self.student)
        other_session = make_session(title="Second session")
        attempt_url = reverse("quiz:attempt_question", args=[self.attempt.pk, 1])
        self.check_pages(
            {
                "dashboard": ("get", reverse("quiz:dashboard"), {}),
                "start": ("get", reverse("quiz:session_start", args=[other_session.pk]), {}),
                "question": ("get", attempt_url, {}),
                "context question": (
                    "get",
                    reverse("quiz:attempt_question", args=[self.attempt.pk, 45]),
                    {},
                ),
                "finish": ("get", reverse("quiz:attempt_finish", args=[self.attempt.pk]), {}),
            }
        )
        # Қате жауап жіберілгендегі хабарлама
        response = self.client.post(attempt_url, {"answer": "abc"}, follow=True)
        self.assert_russian(response, "answer error")

        finish_attempt(self.attempt)
        result_url = reverse("quiz:attempt_result", args=[self.attempt.pk])
        self.assert_russian(self.client.get(result_url), "result")
        self.session.show_answers = ExamSession.ShowAnswers.AFTER_CLOSE
        self.session.save()
        self.assert_russian(self.client.get(result_url), "result without answers")

    def test_teacher_pages(self):
        self.client.force_login(self.teacher)
        question = Question.objects.filter(context__isnull=True).first()
        quiz_context = Context.objects.first()
        self.check_pages(
            {
                "questions": ("get", reverse("quiz:teacher_questions"), {}),
                "question new": ("get", reverse("quiz:teacher_question_create"), {}),
                "question errors": ("post", reverse("quiz:teacher_question_create"), {}),
                "question edit": (
                    "get",
                    reverse("quiz:teacher_question_edit", args=[question.pk]),
                    {},
                ),
                "contexts": ("get", reverse("quiz:teacher_contexts"), {}),
                "context new": ("get", reverse("quiz:teacher_context_create"), {}),
                "context errors": ("post", reverse("quiz:teacher_context_create"), {}),
                "context edit": (
                    "get",
                    reverse("quiz:teacher_context_edit", args=[quiz_context.pk]),
                    {},
                ),
                "bank": ("get", reverse("quiz:teacher_bank"), {}),
                "bank sample": ("get", reverse("quiz:teacher_bank_sample"), {"lang": "ru"}),
                "results": ("get", reverse("quiz:teacher_results"), {}),
                "attempt result": (
                    "get",
                    reverse("quiz:attempt_result", args=[self.attempt.pk]),
                    {},
                ),
            }
        )

    def test_check_finds_kazakh_text(self):
        """Тексерудің өзі жұмыс істейді: қазақша бетте қазақ әріптері табылады."""
        self.client.cookies["django_language"] = "kk"
        self.client.force_login(self.student)
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertRegex(visible_text(response), KAZAKH_ONLY_LETTERS)

    def test_kazakh_form_errors_are_not_in_english(self):
        """Django-ның дайын қате хабарлары қазақша бетте ағылшынша шықпауы керек."""
        self.client.cookies["django_language"] = "kk"
        english = ["This field", "Select a valid", "Enter a valid", "Ensure this", "password"]
        responses = {
            "login": self.client.post(reverse("accounts:login"), {}),
        }
        self.client.force_login(self.teacher)
        responses["question"] = self.client.post(
            reverse("quiz:teacher_question_create"), {"subtopic": "999", "level": "X"}
        )
        responses["context"] = self.client.post(reverse("quiz:teacher_context_create"), {})
        for name, response in responses.items():
            text = re.sub(r"<[^>]+>", " ", visible_text(response))
            for phrase in english:
                with self.subTest(page=name, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_practice_pages(self):
        self.client.force_login(self.student)
        topic = Topic.objects.get(number=8)
        self.assert_russian(self.client.get(reverse("quiz:practice_start")), "practice start")
        self.client.post(reverse("quiz:practice_start"), {"topic": topic.pk, "language": "ru"})
        url = reverse("quiz:practice_question", args=[1])
        self.assert_russian(self.client.get(url), "practice question")
        answer_id = self.client.session["practice"]["items"][0]["answer_order"][0]
        self.client.post(url, {"answer": answer_id})
        self.assert_russian(self.client.get(url), "practice feedback")
        self.assert_russian(self.client.get(reverse("quiz:practice_result")), "practice result")


# ---------- Екінші кезең: тақырыптық жаттығу ----------


class PracticeTests(VariantTestCase):
    def setUp(self):
        self.student = make_student()
        self.client.force_login(self.student)
        # 08 тақырып: 4 тақырыпша — сұрақ көп
        self.topic = Topic.objects.get(number=8)

    def start(self, topic=None, language="kk"):
        return self.client.post(
            reverse("quiz:practice_start"),
            {"topic": (topic or self.topic).pk, "language": language},
        )

    def practice(self):
        return self.client.session["practice"]

    def question_url(self, number):
        return reverse("quiz:practice_question", args=[number])

    def item_question(self, number):
        return Question.objects.get(pk=self.practice()["items"][number - 1]["question"])

    def section(self, response, number):
        """Беттегі n-сұрақтың блогы (<section id="question-n">) HTML мәтіні."""
        content = response.content.decode()
        start = content.index(f'id="question-{number}"')
        end = content.find("<section", start)
        return content[start : end if end != -1 else len(content)]

    def test_start_page(self):
        response = self.client.get(reverse("quiz:practice_start"))
        self.assertContains(response, f"{PRACTICE_QUESTIONS} сұрақ")
        self.assertEqual(len(response.context["form"].fields["topic"].queryset), 11)

    def test_start_chooses_10_questions_from_topic_and_language(self):
        response = self.start(language="ru")
        self.assertRedirects(response, self.question_url(1))
        items = self.practice()["items"]
        self.assertEqual(len(items), PRACTICE_QUESTIONS)
        question_ids = [item["question"] for item in items]
        self.assertEqual(len(set(question_ids)), PRACTICE_QUESTIONS)
        questions = Question.objects.filter(pk__in=question_ids)
        self.assertFalse(questions.exclude(subtopic__topic=self.topic).exists())
        self.assertFalse(questions.exclude(language="ru").exists())
        self.assertFalse(questions.filter(is_active=False).exists())
        for item in items:
            question = Question.objects.get(pk=item["question"])
            self.assertCountEqual(
                item["answer_order"], question.answers.values_list("id", flat=True)
            )
        # Жаттығу Attempt жасамайды
        self.assertFalse(Attempt.objects.exists())

    def test_small_topic_uses_all_available_questions(self):
        topic_questions = Question.objects.filter(subtopic__topic=self.topic, language="kk")
        keep = list(topic_questions.values_list("id", flat=True)[:3])
        topic_questions.exclude(pk__in=keep).update(is_active=False)
        self.start()
        self.assertCountEqual([item["question"] for item in self.practice()["items"]], keep)

    def test_topic_without_questions(self):
        Question.objects.filter(subtopic__topic=self.topic, language="kk").update(is_active=False)
        response = self.start()
        self.assertRedirects(
            response, reverse("quiz:practice_start"), fetch_redirect_response=False
        )
        self.assertNotIn("practice", self.client.session)
        self.assertContains(self.client.get(response["Location"]), "сұрақ әлі жоқ")

    def test_correct_answer_hidden_until_answered(self):
        self.start()
        response = self.client.get(self.question_url(1))
        questions = response.context["questions"]
        self.assertContains(response, 'name="answer"', count=4 * len(questions))
        self.assertNotContains(response, "list-group-item-success")
        for row in questions:
            for answer in row["answers"]:
                self.assertIsNone(answer["is_correct"])
        # «дұрыс» белгісі әр нұсқада бар, бірақ бәрі жасырын
        block = self.section(response, 1)
        self.assertEqual(block.count("badge-correct d-none"), 4)
        self.assertIn("feedback-correct d-none", block)

    def test_correct_answer_feedback(self):
        self.start()
        correct = self.item_question(1).answers.get(is_correct=True)
        response = self.client.post(self.question_url(1), {"answer": correct.pk})
        self.assertRedirects(response, self.question_url(1))
        response = self.client.get(self.question_url(1))
        block = self.section(response, 1)
        self.assertIn('alert alert-success mb-0 feedback-correct"', block)
        self.assertIn("feedback-wrong d-none", block)
        self.assertEqual(block.count("list-group-item-success"), 1)
        self.assertNotContains(response, "list-group-item-danger")
        # Жауап берілген сұрақта таңдау өшірулі
        self.assertEqual(block.count(" disabled>"), 4)
        self.assertEqual(response.context["navigation"][0]["state"], "correct")

    def test_wrong_answer_feedback_and_cannot_change(self):
        self.start()
        question = self.item_question(2)
        wrong = question.answers.filter(is_correct=False).first()
        correct = question.answers.get(is_correct=True)
        self.client.post(self.question_url(2), {"answer": wrong.pk})
        # Жауапты өзгертуге болмайды
        self.client.post(self.question_url(2), {"answer": correct.pk})
        self.assertEqual(self.practice()["items"][1]["selected"], wrong.pk)

        response = self.client.get(self.question_url(2))
        block = self.section(response, 2)
        self.assertIn('alert alert-danger mb-0 feedback-wrong"', block)
        self.assertContains(response, "list-group-item-success", count=1)
        self.assertContains(response, "list-group-item-danger", count=1)
        self.assertEqual(response.context["navigation"][1]["state"], "wrong")

    def test_answer_from_other_question_is_rejected(self):
        self.start()
        other = self.item_question(2).answers.first()
        self.client.post(self.question_url(1), {"answer": other.pk})
        self.assertIsNone(self.practice()["items"][0]["selected"])

    def test_result(self):
        self.start()
        for number in [1, 2]:
            answer = self.item_question(number).answers.get(is_correct=True)
            self.client.post(self.question_url(number), {"answer": answer.pk})
        wrong = self.item_question(3).answers.filter(is_correct=False).first()
        self.client.post(self.question_url(3), {"answer": wrong.pk})

        response = self.client.get(reverse("quiz:practice_result"))
        summary = response.context["summary"]
        self.assertEqual(summary["correct"], 2)
        self.assertEqual(summary["total"], PRACTICE_QUESTIONS)
        self.assertEqual(summary["unanswered"], PRACTICE_QUESTIONS - 3)
        self.assertEqual(summary["topic"], self.topic)
        self.assertContains(response, f"2 / {PRACTICE_QUESTIONS}")
        self.assertContains(response, "Дұрыс жауаптар: 20%.")

    def test_new_practice_replaces_old(self):
        self.start()
        first_answer = self.item_question(1).answers.first()
        self.client.post(self.question_url(1), {"answer": first_answer.pk})
        self.start(topic=Topic.objects.get(number=1))
        self.assertTrue(all(item["selected"] is None for item in self.practice()["items"]))

    def test_without_practice_or_wrong_number(self):
        for url in [self.question_url(1), reverse("quiz:practice_result")]:
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), reverse("quiz:practice_start"))
        self.start()
        self.assertEqual(self.client.get(self.question_url(0)).status_code, 404)
        last = PRACTICE_QUESTIONS + 1
        self.assertEqual(self.client.get(self.question_url(last)).status_code, 404)

    def test_deleted_question_restarts_practice(self):
        self.start()
        session = self.client.session
        session["practice"]["items"][0]["question"] = 999999
        session.save()
        response = self.client.get(self.question_url(1))
        self.assertRedirects(response, reverse("quiz:practice_start"))
        self.assertNotIn("practice", self.client.session)

    def test_guest_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("quiz:practice_start"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_links_from_navigation_and_dashboard(self):
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, reverse("quiz:practice_start"), count=2)


# ---------- Жүктеме тестінің деректері (loadtest_data) ----------


class LoadtestDataTests(VariantTestCase):
    def run_loadtest_data(self, *args):
        out = StringIO()
        call_command("loadtest_data", *args, stdout=out)
        return out.getvalue()

    def loadtest_session(self):
        return ExamSession.objects.get(title=LOADTEST_SESSION_TITLE)

    def test_creates_accounts_and_open_session(self):
        output = self.run_loadtest_data("--count", "5")
        users = User.objects.filter(username__startswith="student").order_by("username")
        self.assertEqual(
            list(users.values_list("username", flat=True)),
            ["student001", "student002", "student003", "student004", "student005"],
        )
        group = StudyGroup.objects.get(name="LOADTEST")
        for user in users:
            self.assertTrue(user.check_password(LOADTEST_PASSWORD))
            self.assertFalse(user.is_staff)
            self.assertEqual(user.profile.group, group)

        session = self.loadtest_session()
        self.assertTrue(is_session_open(session))
        self.assertEqual(list(session.groups.all()), [group])
        self.assertIn(f"LOADTEST_SESSION_ID={session.pk}", output)

        # Нақты студент бұл сессияны көрмейді
        student = User.objects.create_user(username="real", password="pass12345")
        self.assertNotIn(session, visible_sessions(student))

    def test_account_can_log_in_and_start_test(self):
        self.run_loadtest_data("--count", "1")
        session = self.loadtest_session()
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "student001", "password": LOADTEST_PASSWORD},
            follow=True,
        )
        self.assertEqual(response.request["PATH_INFO"], reverse("quiz:dashboard"))
        response = self.client.post(
            reverse("quiz:session_start", args=[session.pk]), {"language": "kk"}
        )
        attempt = Attempt.objects.get(user__username="student001")
        self.assertRedirects(response, reverse("quiz:attempt_question", args=[attempt.pk, 1]))

    def test_rerun_resets_attempts_without_duplicates(self):
        self.run_loadtest_data("--count", "3")
        session = self.loadtest_session()
        create_attempt(User.objects.get(username="student001"), session, "kk")
        ExamSession.objects.filter(pk=session.pk).update(
            closes_at=timezone.now() - timedelta(minutes=1)
        )

        output = self.run_loadtest_data("--count", "3")
        self.assertEqual(User.objects.filter(username__startswith="student").count(), 3)
        self.assertFalse(Attempt.objects.exists())
        self.assertIn("ескі әрекеттер: 1", output)
        session = self.loadtest_session()
        self.assertTrue(is_session_open(session))
        self.assertEqual(ExamSession.objects.filter(title=LOADTEST_SESSION_TITLE).count(), 1)

    def test_foreign_account_is_not_changed(self):
        real = User.objects.create_user(username="student002", password="real-password-1")
        with self.assertRaises(CommandError):
            self.run_loadtest_data("--count", "3")
        real.refresh_from_db()
        self.assertTrue(real.check_password("real-password-1"))
        self.assertFalse(User.objects.filter(username="student001").exists())

    def test_delete_removes_only_loadtest_data(self):
        real = User.objects.create_user(username="real", password="pass12345")
        self.run_loadtest_data("--count", "2")
        create_attempt(User.objects.get(username="student001"), self.loadtest_session(), "kk")

        output = self.run_loadtest_data("--delete")
        self.assertIn("2 аккаунт, 1 әрекет", output)
        self.assertFalse(User.objects.filter(username__startswith="student").exists())
        self.assertFalse(StudyGroup.objects.filter(name="LOADTEST").exists())
        self.assertFalse(ExamSession.objects.filter(title=LOADTEST_SESSION_TITLE).exists())
        self.assertTrue(User.objects.filter(pk=real.pk).exists())

    def test_usernames_for_1000_accounts(self):
        names = loadtest_usernames(1000)
        self.assertEqual(names[0], "student001")
        self.assertEqual(names[99], "student100")
        self.assertEqual(names[-1], "student1000")
        self.assertEqual(len(set(names)), 1000)


# ---------- 9-кезең: пәндер ----------


class LoadSubjectsTests(TestCase):
    # Тақырыптар саны (TZ.md, 10.1)
    EXPECTED_TOPICS = {
        "informatics": 11,
        "art_labor_boys": 5,
        "art_labor_girls": 5,
        "mathematics": 20,
    }

    def test_loads_4_subjects(self):
        run_command("load_subjects")
        self.assertEqual(
            list(Subject.objects.values_list("code", flat=True)), list(self.EXPECTED_TOPICS)
        )
        for code, topic_count in self.EXPECTED_TOPICS.items():
            with self.subTest(code=code):
                subject = Subject.objects.get(code=code)
                self.assertEqual(subject.topics.count(), topic_count)
                numbers = Subtopic.objects.filter(topic__subject=subject).values_list(
                    "number", flat=True
                )
                self.assertEqual(sorted(numbers), list(range(1, SUBTOPICS_COUNT + 1)))

    def test_subject_settings(self):
        run_command("load_subjects")
        durations = dict(Subject.objects.values_list("code", "duration_minutes"))
        self.assertEqual(
            durations,
            {"informatics": 125, "art_labor_boys": 80, "art_labor_girls": 80, "mathematics": 125},
        )
        self.assertTrue(Subject.objects.get(code="mathematics").uses_formulas)
        self.assertFalse(Subject.objects.get(code="informatics").uses_formulas)
        # Толық мазмұны да жүктеледі
        subtopic = Subtopic.objects.get(topic__subject__code="mathematics", number=1)
        self.assertNotEqual(subtopic.description_kk, "")

    def test_is_idempotent(self):
        run_command("load_subjects")
        run_command("load_subjects")
        self.assertEqual(Subject.objects.count(), 4)
        self.assertEqual(Topic.objects.count(), sum(self.EXPECTED_TOPICS.values()))
        self.assertEqual(Subtopic.objects.count(), 4 * SUBTOPICS_COUNT)

    def test_only_one_subject(self):
        run_command("load_subjects", "--only", "mathematics")
        self.assertEqual(
            set(Subject.objects.values_list("code", flat=True)), {"informatics", "mathematics"}
        )
        self.assertEqual(Topic.objects.filter(subject__code="mathematics").count(), 20)
        # Информатика пәні миграцияда жасалған, тақырыптары жүктелмеген
        self.assertFalse(Topic.objects.filter(subject__code="informatics").exists())

    def test_unknown_subject(self):
        with self.assertRaises(CommandError):
            run_command("load_subjects", "--only", "physics")

    def test_load_topics_loads_only_informatics(self):
        run_command("load_topics")
        self.assertEqual(Subject.objects.count(), 1)
        self.assertEqual(Topic.objects.filter(subject=informatics()).count(), 11)
        self.assertEqual(Subtopic.objects.count(), SUBTOPICS_COUNT)

    def test_existing_informatics_is_updated_not_duplicated(self):
        subject = informatics()
        run_command("load_subjects")
        self.assertEqual(Subject.objects.get(code=INFORMATICS_CODE).pk, subject.pk)

    def check_invalid(self, change):
        """Деректерді бұзып жүктейді: қате шығып, ештеңе жазылмауы керек."""
        subjects = read_subjects()
        change(subjects)
        path = "apps.quiz.management.commands.load_subjects.read_subjects"
        with mock.patch(path, return_value=subjects):
            with self.assertRaises(CommandError):
                run_command("load_subjects")
        self.assertEqual(Subject.objects.count(), 1)  # миграциядағы информатика ғана
        self.assertFalse(Topic.objects.exists())
        self.assertFalse(Subtopic.objects.exists())

    def test_rejects_19_subtopics(self):
        # Математиканың соңғы тақырыпшасы жоқ: басқа пәндер дұрыс болса да жазылмайды
        self.check_invalid(lambda subjects: subjects[3]["topics"][-1]["subtopics"].pop())

    def test_rejects_gap_in_numbers(self):
        def change(subjects):
            subjects[1]["topics"][0]["subtopics"][0]["number"] = 21

        self.check_invalid(change)

    def test_rejects_empty_name(self):
        def change(subjects):
            subjects[2]["topics"][0]["subtopics"][0]["name_ru"] = " "

        self.check_invalid(change)


class SubjectModelTests(TestCase):
    def setUp(self):
        run_command("load_subjects")
        self.mathematics = Subject.objects.get(code="mathematics")

    def test_topic_number_is_unique_within_subject(self):
        with self.assertRaises(IntegrityError):
            Topic.objects.create(subject=self.mathematics, number=1, name_kk="А", name_ru="А")

    def test_subtopic_numbers_repeat_across_subjects(self):
        self.assertEqual(Subtopic.objects.filter(number=1).count(), 4)

    def test_context_subject_must_match_question_subject(self):
        context = Context.objects.create(
            subject=self.mathematics, language="kk", title="Кесте", text="Мәтін"
        )
        subtopic = Subtopic.objects.get(topic__subject=informatics(), number=1)
        question = Question(
            subtopic=subtopic, context=context, language="kk", text="Сұрақ", level="B"
        )
        with self.assertRaises(ValidationError):
            question.full_clean()
        # Сол пәннің тақырыпшасымен — дұрыс
        question.subtopic = Subtopic.objects.get(topic__subject=self.mathematics, number=1)
        question.full_clean()

    def test_name_follows_interface_language(self):
        subject = Subject.objects.get(code="art_labor_boys")
        with override("ru"):
            self.assertEqual(str(subject), subject.name_ru)
        with override("kk"):
            self.assertEqual(str(subject), subject.name_kk)


class SessionSubjectFormTests(TestCase):
    def setUp(self):
        run_command("load_subjects")
        self.mathematics = Subject.objects.get(code="mathematics")
        self.math_group = StudyGroup.objects.create(name="МАТ-21", subject=self.mathematics, language="kk")
        self.inf_group = StudyGroup.objects.create(name="ИНФ-21", subject=informatics(), language="kk")

    def form(self, *groups):
        today = timezone.localdate().strftime("%Y-%m-%d")
        return ExamSessionAdminForm(
            {
                "title": "Сессия",
                "subject": self.mathematics.pk,
                "opens_at": f"{today} 09:00",
                "closes_at": f"{today} 18:00",
                "groups": [group.pk for group in groups],
                "show_answers": ExamSession.ShowAnswers.AFTER_FINISH,
                "is_active": "on",
            }
        )

    def test_groups_of_same_subject(self):
        form = self.form(self.math_group)
        self.assertTrue(form.is_valid(), form.errors)

    def test_group_of_other_subject_is_rejected(self):
        form = self.form(self.math_group, self.inf_group)
        self.assertFalse(form.is_valid())
        self.assertIn("ИНФ-21", str(form.errors["groups"]))


class SubjectDurationTests(TakeTestCase):
    """Тест уақыты сессияның пәнінен алынады (көркем еңбек — 80 мин)."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        # Көркем еңбектің банкі жоқ: мерзімді attempt_deadline() арқылы тексереміз
        self.art_labor = Subject.objects.create(
            code="art_labor_boys",
            name_kk="Көркем еңбек",
            name_ru="Художественный труд",
            duration_minutes=80,
        )
        self.art_session = make_session(subject=self.art_labor)

    def test_art_labor_deadline_is_80_minutes(self):
        deadline = attempt_deadline(self.art_session, self.now)
        self.assertEqual(deadline, self.now + timedelta(minutes=80))

    def test_informatics_deadline_is_125_minutes(self):
        attempt = create_attempt(self.student, self.session, "kk", now=self.now)
        self.assertEqual(attempt.deadline, self.now + timedelta(minutes=125))

    def test_deadline_does_not_exceed_session_close(self):
        self.art_session.closes_at = self.now + timedelta(minutes=50)
        self.assertEqual(attempt_deadline(self.art_session, self.now), self.art_session.closes_at)

    def test_start_page_shows_subject_duration(self):
        # Көркем еңбек сессиясын тек сол пәннің тобындағы студент көреді
        self.student.profile.group = StudyGroup.objects.create(name="КЕ-21", subject=self.art_labor, language="kk")
        self.student.profile.save()
        response = self.client.get(reverse("quiz:session_start", args=[self.art_session.pk]))
        self.assertContains(response, "80 минут")
        self.assertNotContains(response, "125 минут")


class TeacherContextSubjectTests(TeacherTestCase):
    def test_new_context_belongs_to_first_subject(self):
        self.client.post(
            reverse("quiz:teacher_context_create"),
            {"title": "Кесте", "language": "kk", "text": "Мәтін", "is_active": "on"},
        )
        self.assertEqual(Context.objects.get().subject, informatics())


class InformaticsMigrationTests(TransactionTestCase):
    """Деректер миграциясы: бұрынғы жазбалар «Информатикаға» байланады, ештеңе жоғалмайды."""

    # Тест соңында миграция жасаған деректер (информатика пәні) қалпына келеді
    serialized_rollback = True

    before = [("quiz", "0001_initial"), ("accounts", "0001_initial")]
    after = [("quiz", "0004_subject_required"), ("accounts", "0003_subject_required")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Барлық миграцияны қайта қолданамыз (келесі тесттер үшін)
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_old_data_is_bound_to_informatics(self):
        old = self.migrate(self.before)
        now = timezone.now()
        group = old.get_model("accounts", "StudyGroup").objects.create(name="ИНФ-21")
        topic = old.get_model("quiz", "Topic").objects.create(number=1, name_kk="Т", name_ru="Т")
        old.get_model("quiz", "Subtopic").objects.create(
            topic=topic, number=1, name_kk="Т", name_ru="Т"
        )
        old.get_model("quiz", "Context").objects.create(language="kk", title="К", text="М")
        session = old.get_model("quiz", "ExamSession").objects.create(
            title="Сессия", opens_at=now, closes_at=now + timedelta(hours=2)
        )
        session.groups.add(group)
        old_users = old.get_model("auth", "User").objects
        old_profiles = old.get_model("accounts", "Profile").objects
        teacher = old_users.create(username="teacher", is_staff=True)
        student = old_users.create(username="student")
        old_profiles.create(user=teacher)
        old_profiles.create(user=student, group=group)
        old.get_model("quiz", "Attempt").objects.create(
            user=student, session=session, language="kk", deadline=now, score=10
        )

        new = self.migrate(self.after)
        subject = new.get_model("quiz", "Subject").objects.get(code="informatics")
        self.assertEqual(subject.duration_minutes, 125)
        for app_label, model_name in [
            ("quiz", "Topic"),
            ("quiz", "Context"),
            ("quiz", "ExamSession"),
            ("accounts", "StudyGroup"),
        ]:
            with self.subTest(model=model_name):
                objects = new.get_model(app_label, model_name).objects
                self.assertEqual(objects.count(), 1)
                self.assertEqual(objects.get().subject_id, subject.pk)
        self.assertEqual(new.get_model("quiz", "Subtopic").objects.count(), 1)
        self.assertEqual(new.get_model("quiz", "Attempt").objects.get().score, 10)
        new_session = new.get_model("quiz", "ExamSession").objects.get()
        self.assertEqual(new_session.groups.count(), 1)

        profiles = new.get_model("accounts", "Profile").objects
        self.assertEqual(list(profiles.get(user__username="teacher").subjects.all()), [subject])
        self.assertFalse(profiles.get(user__username="student").subjects.exists())


# ---------- 10-кезең: оқытушы жағы (пән бойынша) ----------


class SubjectTeacherTestCase(TestCase):
    """
    4 пән жүктелген. Информатика мен математиканың әрқайсында бір сұрақ,
    бір контекст, бір топ, бір сессия және бір аяқталған әрекет бар.
    """

    def setUp(self):
        run_command("load_subjects")
        self.informatics = informatics()
        self.mathematics = Subject.objects.get(code="mathematics")
        self.inf = self.subject_data(self.informatics, "ИНФ-21")
        self.math = self.subject_data(self.mathematics, "МАТ-21")

    def subject_data(self, subject, group_name):
        """Пәннің сұрағы, контексті, тобы, сессиясы және әрекеті."""
        subtopic = Subtopic.objects.get(topic__subject=subject, number=1)
        question = Question.objects.create(
            subtopic=subtopic, language="kk", text=f"{subject.code} сұрағы", level="A"
        )
        for index in range(ANSWERS_PER_QUESTION):
            Answer.objects.create(question=question, text=f"Жауап {index}", is_correct=index == 0)
        context = Context.objects.create(
            subject=subject, language="kk", title=f"{subject.code} контексті", text="Мәтін"
        )
        group = StudyGroup.objects.create(name=group_name, subject=subject, language="kk")
        session = make_session(title=f"{subject.code} сессиясы", subject=subject)
        student = User.objects.create_user(username=f"{subject.code}-student", password="pass12345")
        student.profile.group = group
        student.profile.save()
        now = timezone.now()
        attempt = Attempt.objects.create(
            user=student,
            session=session,
            language="kk",
            started_at=now - timedelta(minutes=30),
            deadline=now + timedelta(minutes=30),
            finished_at=now - timedelta(minutes=5),
            status=Attempt.Status.FINISHED,
            score=10,
        )
        return {
            "subtopic": subtopic,
            "question": question,
            "context": context,
            "group": group,
            "session": session,
            "attempt": attempt,
        }

    def login_teacher(self, *subjects):
        teacher = make_teacher(username="t-" + "-".join(s.code for s in subjects), subjects=subjects)
        self.client.force_login(teacher)
        return teacher

    def select_subject(self, subject, next_url="/teacher/questions/"):
        return self.client.post(
            reverse("quiz:teacher_subject_select"), {"subject": subject.pk, "next": next_url}
        )


class TeacherSubjectAccessTests(SubjectTeacherTestCase):
    """Математика оқытушысы информатиканың деректерін көрмейді (TZ.md, 10.9)."""

    def setUp(self):
        super().setUp()
        self.login_teacher(self.mathematics)

    def test_question_list_shows_only_own_subject(self):
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertContains(response, "mathematics сұрағы")
        self.assertNotContains(response, "informatics сұрағы")

    def test_other_subject_pages_are_404(self):
        question = self.inf["question"].pk
        pages = [
            ("get", reverse("quiz:teacher_question_edit", args=[question])),
            ("post", reverse("quiz:teacher_question_copy", args=[question])),
            ("post", reverse("quiz:teacher_question_toggle", args=[question])),
            ("get", reverse("quiz:teacher_context_edit", args=[self.inf["context"].pk])),
            ("get", reverse("quiz:attempt_result", args=[self.inf["attempt"].pk])),
        ]
        for method, url in pages:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 404)
        # Ештеңе өзгермеді
        self.assertEqual(Question.objects.count(), 2)
        self.assertTrue(Question.objects.get(pk=question).is_active)

    def test_own_subject_pages_open(self):
        pages = [
            reverse("quiz:teacher_question_edit", args=[self.math["question"].pk]),
            reverse("quiz:teacher_context_edit", args=[self.math["context"].pk]),
            reverse("quiz:attempt_result", args=[self.math["attempt"].pk]),
        ]
        for url in pages:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_context_list_shows_only_own_subject(self):
        response = self.client.get(reverse("quiz:teacher_contexts"))
        self.assertContains(response, "mathematics контексті")
        self.assertNotContains(response, "informatics контексті")

    def test_results_and_csv_show_only_own_subject(self):
        response = self.client.get(reverse("quiz:teacher_results"))
        self.assertEqual([row["attempt"] for row in response.context["rows"]], [self.math["attempt"]])
        form = response.context["filter_form"]
        self.assertEqual(list(form.fields["session"].queryset), [self.math["session"]])
        self.assertEqual(list(form.fields["group"].queryset), [self.math["group"]])

        content = self.client.get(reverse("quiz:teacher_results_export")).content.decode()
        self.assertIn("mathematics-student", content)
        self.assertIn("Математика", content)
        self.assertNotIn("informatics-student", content)

    def test_results_filter_ignores_other_subject_session(self):
        response = self.client.get(
            reverse("quiz:teacher_results"), {"session": self.inf["session"].pk}
        )
        # Бөтен сессия сүзгіде жоқ — сүзгі қолданылмайды, бірақ бөтен нәтиже шықпайды
        self.assertEqual([row["attempt"] for row in response.context["rows"]], [self.math["attempt"]])

    def test_bank_shows_subject_subtopics(self):
        response = self.client.get(reverse("quiz:teacher_bank"))
        rows = response.context["coverage"]["rows"]
        self.assertEqual(len(rows), SUBTOPICS_COUNT)
        self.assertTrue(all(row["subtopic"].topic.subject_id == self.mathematics.pk for row in rows))

    def test_bank_sample_without_bank_shows_error(self):
        response = self.client.get(reverse("quiz:teacher_bank_sample"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("error", response.context)

    def test_new_context_belongs_to_selected_subject(self):
        self.client.post(
            reverse("quiz:teacher_context_create"),
            {"title": "Жаңа", "language": "kk", "text": "Мәтін", "is_active": "on"},
        )
        self.assertEqual(Context.objects.get(title="Жаңа").subject, self.mathematics)


class TeacherQuestionFormSubjectTests(SubjectTeacherTestCase):
    def setUp(self):
        super().setUp()
        self.login_teacher(self.mathematics)
        Context.objects.create(subject=self.mathematics, language="ru", title="Орысша", text="Т")

    def test_form_lists_only_subject_topics_and_contexts(self):
        response = self.client.get(reverse("quiz:teacher_question_create"))
        form = response.context["form"]
        self.assertEqual(form.fields["topic"].queryset.count(), 20)
        self.assertFalse(
            form.fields["topic"].queryset.exclude(subject=self.mathematics).exists()
        )
        self.assertEqual(form.fields["subtopic"].queryset.count(), SUBTOPICS_COUNT)
        contexts = set(form.fields["context"].queryset)
        self.assertIn(self.math["context"], contexts)
        self.assertNotIn(self.inf["context"], contexts)
        # Контекст тізімінде тілі жазылады (JS сол тілдікін ғана қалдырады)
        self.assertContains(response, 'data-language="ru"')
        self.assertContains(response, "js/context_filter.js")

    def test_subtopic_description_is_hint(self):
        response = self.client.get(reverse("quiz:teacher_question_create"))
        description = self.math["subtopic"].description_kk
        self.assertNotEqual(description, "")
        self.assertContains(response, f'title="{escape(description)}"')

    def test_other_subject_subtopic_is_rejected(self):
        response = self.client.post(
            reverse("quiz:teacher_question_create"),
            {
                "topic": self.inf["subtopic"].topic_id,
                "subtopic": self.inf["subtopic"].pk,
                "level": "A",
                "language": "kk",
                "context": "",
                "text": "Бөтен пәнге сұрақ",
                "answers-TOTAL_FORMS": "4",
                "answers-INITIAL_FORMS": "0",
                "answers-MIN_NUM_FORMS": "4",
                "answers-MAX_NUM_FORMS": "4",
                "answers-0-text": "1",
                "answers-1-text": "2",
                "answers-2-text": "3",
                "answers-3-text": "4",
                "answers-correct": "0",
                "save": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("subtopic", response.context["form"].errors)
        self.assertFalse(Question.objects.filter(text="Бөтен пәнге сұрақ").exists())

    def test_question_list_filter_lists_only_subject_topics(self):
        response = self.client.get(reverse("quiz:teacher_questions"))
        form = response.context["filter_form"]
        self.assertEqual(form.fields["topic"].queryset.count(), 20)
        self.assertEqual(form.fields["subtopic"].queryset.count(), SUBTOPICS_COUNT)


class TeacherSubjectSwitchTests(SubjectTeacherTestCase):
    def test_teacher_without_subject_sees_message(self):
        self.login_teacher()
        urls = [
            reverse("quiz:teacher_questions"),
            reverse("quiz:teacher_question_create"),
            reverse("quiz:teacher_contexts"),
            reverse("quiz:teacher_bank"),
            reverse("quiz:teacher_bank_sample"),
            reverse("quiz:teacher_results"),
            reverse("quiz:teacher_results_export"),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(
                    response, "Сізге пән тағайындалмаған, әкімшіге хабарласыңыз.", status_code=403
                )
        # Нәтиже беті де ашылмайды
        url = reverse("quiz:attempt_result", args=[self.inf["attempt"].pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_no_subject_message_in_russian(self):
        self.login_teacher()
        self.client.cookies["django_language"] = "ru"
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertContains(
            response, "Вам не назначен предмет, обратитесь к администратору.", status_code=403
        )

    def test_one_subject_shows_name_without_switcher(self):
        self.login_teacher(self.mathematics)
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertContains(response, "Математика")
        self.assertNotContains(response, reverse("quiz:teacher_subject_select"))

    def test_default_is_first_subject(self):
        self.login_teacher(self.mathematics, self.informatics)
        response = self.client.get(reverse("quiz:teacher_questions"))
        # Реті бойынша бірінші — информатика
        self.assertContains(response, "informatics сұрағы")
        self.assertContains(response, reverse("quiz:teacher_subject_select"))
        self.assertContains(response, "Математика")

    def test_switch_subject(self):
        self.login_teacher(self.mathematics, self.informatics)
        response = self.select_subject(self.mathematics, "/teacher/questions/?topic=3&page=2")
        # Сүзгі параметрлері алынып тасталады (олар бұрынғы пәнге жатады)
        self.assertRedirects(response, reverse("quiz:teacher_questions"))
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertContains(response, "mathematics сұрағы")
        self.assertNotContains(response, "informatics сұрағы")
        # Таңдау келесі беттерде де сақталады
        response = self.client.get(reverse("quiz:teacher_results"))
        self.assertEqual([row["attempt"] for row in response.context["rows"]], [self.math["attempt"]])

    def test_switch_from_edit_page_returns_to_list(self):
        self.login_teacher(self.mathematics, self.informatics)
        edit_url = reverse("quiz:teacher_question_edit", args=[self.inf["question"].pk])
        response = self.select_subject(self.mathematics, edit_url)
        self.assertRedirects(response, reverse("quiz:teacher_questions"))
        edit_url = reverse("quiz:teacher_context_edit", args=[self.inf["context"].pk])
        response = self.select_subject(self.mathematics, edit_url)
        self.assertRedirects(response, reverse("quiz:teacher_contexts"))

    def test_switch_keeps_section(self):
        self.login_teacher(self.mathematics, self.informatics)
        response = self.select_subject(self.mathematics, reverse("quiz:teacher_bank"))
        self.assertRedirects(response, reverse("quiz:teacher_bank"))

    def test_external_next_is_ignored(self):
        self.login_teacher(self.mathematics, self.informatics)
        response = self.select_subject(self.mathematics, "https://evil.example/")
        self.assertRedirects(response, reverse("quiz:teacher_questions"))

    def test_cannot_select_unassigned_subject(self):
        self.login_teacher(self.mathematics)
        self.select_subject(self.informatics)
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertContains(response, "mathematics сұрағы")
        self.assertNotContains(response, "informatics сұрағы")

    def test_select_requires_post_and_staff(self):
        self.login_teacher(self.mathematics)
        self.assertEqual(self.client.get(reverse("quiz:teacher_subject_select")).status_code, 405)
        student = User.objects.create_user(username="student", password="pass12345")
        self.client.force_login(student)
        response = self.select_subject(self.mathematics)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_superuser_sees_all_subjects(self):
        admin = User.objects.create_superuser(username="admin", password="pass12345")
        self.client.force_login(admin)
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertEqual(len(response.wsgi_request.teacher_subjects), 4)
        self.select_subject(self.mathematics)
        response = self.client.get(reverse("quiz:attempt_result", args=[self.inf["attempt"].pk]))
        self.assertEqual(response.status_code, 200)

    def test_inactive_subject_is_hidden(self):
        self.mathematics.is_active = False
        self.mathematics.save()
        self.login_teacher(self.mathematics)
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertEqual(response.status_code, 403)


class SubjectVariantTests(VariantTestCase):
    """Басқа пәндер жүктелсе де, информатика нұсқасы тек информатикадан құрылады."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        run_command("load_subjects")
        # Математикаға бөтен сұрақ пен толық контекст қосамыз — нұсқаға түспеуі керек
        mathematics = Subject.objects.get(code="mathematics")
        context = Context.objects.create(subject=mathematics, language="kk", title="М", text="М")
        subtopic = Subtopic.objects.get(topic__subject=mathematics, number=1)
        for level in ["A", "B", "B", "B", "C"]:
            Question.objects.create(
                subtopic=subtopic, context=context, language="kk", text="М", level=level
            )
        Question.objects.create(subtopic=subtopic, language="kk", text="М", level="A")

    def test_variant_uses_only_subject_questions(self):
        for seed in range(5):
            with self.subTest(seed=seed):
                questions = self.variant_questions("kk", seed)
                self.assertEqual(len(questions), QUESTIONS_TOTAL)
                subjects = {question.subtopic.topic.subject_id for question in questions}
                self.assertEqual(subjects, {informatics().pk})

    def test_subject_without_bank_cannot_build_variant(self):
        mathematics = Subject.objects.get(code="mathematics")
        with self.assertLogs("apps.quiz.services", level="WARNING"):
            with self.assertRaises(AttemptError):
                build_variant(mathematics, "kk")

    def test_bank_coverage_is_per_subject(self):
        coverage = bank_coverage(Subject.objects.get(code="mathematics"))
        self.assertEqual(len(coverage["rows"]), SUBTOPICS_COUNT)
        # Математиканың 01-тақырыпшасында бір жеке сұрақ (A, kk)
        self.assertEqual(coverage["rows"][0]["cells"][0]["count"], 1)
        # Толық контекст: kk — 1, ru — 0
        self.assertEqual([item["count"] for item in coverage["contexts"]], [1, 0])


# ---------- 11-кезең: студент жағы (пән — тобынан) ----------


class StudentSubjectTestCase(TestCase):
    """4 пән; ИНФ-21, МАТ-21, КЕ-21 топтары және МАТ-21 тобындағы кірген студент."""

    def setUp(self):
        run_command("load_subjects")
        self.informatics = informatics()
        self.mathematics = Subject.objects.get(code="mathematics")
        self.art_labor = Subject.objects.get(code="art_labor_boys")
        self.inf_group = StudyGroup.objects.create(name="ИНФ-21", subject=self.informatics, language="kk")
        self.math_group = StudyGroup.objects.create(name="МАТ-21", subject=self.mathematics, language="kk")
        self.art_group = StudyGroup.objects.create(name="КЕ-21", subject=self.art_labor, language="kk")
        self.student = make_student(group=self.math_group)
        self.client.force_login(self.student)

    def session_for(self, subject, *groups, **kwargs):
        session = make_session(subject=subject, **kwargs)
        session.groups.set(groups)
        return session


class StudentSessionsBySubjectTests(StudentSubjectTestCase):
    def test_student_sees_only_own_subject_sessions(self):
        math_for_all = self.session_for(self.mathematics, title="Математика барлығына")
        math_own = self.session_for(self.mathematics, self.math_group, title="МАТ-21 сессиясы")
        self.session_for(self.informatics, title="Информатика барлығына")
        self.session_for(self.informatics, self.inf_group, title="ИНФ-21 сессиясы")
        self.assertCountEqual(visible_sessions(self.student), [math_for_all, math_own])

    def test_math_session_for_all_is_hidden_from_inf_group(self):
        math_for_all = self.session_for(self.mathematics, title="Математика барлығына")
        inf_student = make_student(username="inf", group=self.inf_group)
        self.assertEqual(list(visible_sessions(inf_student)), [])
        self.client.force_login(inf_student)
        url = reverse("quiz:session_start", args=[math_for_all.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertNotContains(self.client.get(reverse("quiz:dashboard")), "Математика барлығына")

    def test_dashboard_shows_subject_and_duration(self):
        self.session_for(self.mathematics, title="Математика сессиясы")
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, "Пән: Математика · Топ: МАТ-21")
        self.assertContains(response, "Математика сессиясы")
        self.assertContains(response, "125 минут")

    def test_art_labor_dashboard_and_start_page_show_80_minutes(self):
        session = self.session_for(self.art_labor, title="Көркем еңбек сессиясы")
        art_student = make_student(username="art", group=self.art_group)
        self.client.force_login(art_student)
        self.assertContains(self.client.get(reverse("quiz:dashboard")), "80 минут")
        response = self.client.get(reverse("quiz:session_start", args=[session.pk]))
        self.assertContains(response, "80 минут")
        self.assertContains(response, self.art_labor.name_kk)

    def test_student_without_group_sees_warning(self):
        self.student.profile.group = None
        self.student.profile.save()
        self.session_for(self.mathematics, title="Математика барлығына")
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, "Сіздің тобыңыз көрсетілмеген")
        self.assertNotContains(response, "Математика барлығына")


class StudentPracticeBySubjectTests(StudentSubjectTestCase):
    def test_practice_lists_only_own_subject_topics(self):
        response = self.client.get(reverse("quiz:practice_start"))
        topics = response.context["form"].fields["topic"].queryset
        self.assertEqual(topics.count(), 20)
        self.assertFalse(topics.exclude(subject=self.mathematics).exists())
        self.assertContains(response, "Пән: Математика")

    def test_other_subject_topic_is_rejected(self):
        inf_topic = Topic.objects.get(subject=self.informatics, number=1)
        response = self.client.post(
            reverse("quiz:practice_start"), {"topic": inf_topic.pk, "language": "kk"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("topic", response.context["form"].errors)
        self.assertNotIn("practice", self.client.session)

    def test_practice_from_own_subject(self):
        topic = Topic.objects.get(subject=self.mathematics, number=1)
        subtopic = topic.subtopics.first()
        question = Question.objects.create(subtopic=subtopic, language="kk", text="2+2?", level="A")
        for index in range(ANSWERS_PER_QUESTION):
            Answer.objects.create(question=question, text=str(index), is_correct=index == 0)
        response = self.client.post(
            reverse("quiz:practice_start"), {"topic": topic.pk, "language": "kk"}
        )
        self.assertRedirects(response, reverse("quiz:practice_question", args=[1]))
        self.assertEqual(self.client.session["practice"]["items"][0]["question"], question.pk)

    def test_without_group_no_topics(self):
        self.student.profile.group = None
        self.student.profile.save()
        response = self.client.get(reverse("quiz:practice_start"))
        self.assertContains(response, "Сіздің тобыңыз көрсетілмеген")
        self.assertNotContains(response, "Жаттығуды бастау")


class StudentResultBySubjectTests(StudentSubjectTestCase):
    def finished_attempt(self, subject, group):
        student = make_student(username=f"s-{subject.code}", group=group)
        session = self.session_for(subject, title=f"{subject.code} сессиясы")
        now = timezone.now()
        attempt = Attempt.objects.create(
            user=student,
            session=session,
            language="kk",
            started_at=now - timedelta(minutes=40),
            deadline=now + timedelta(minutes=40),
            finished_at=now,
            status=Attempt.Status.FINISHED,
            score=0,
        )
        self.client.force_login(student)
        return attempt

    def test_topic_analysis_uses_subject_topics(self):
        cases = [(self.art_labor, self.art_group, 5), (self.mathematics, self.math_group, 20)]
        for subject, group, topic_count in cases:
            with self.subTest(subject=subject.code):
                attempt = self.finished_attempt(subject, group)
                response = self.client.get(reverse("quiz:attempt_result", args=[attempt.pk]))
                topics = response.context["result"]["topics"]
                self.assertEqual(len(topics), topic_count)
                self.assertTrue(all(row["topic"].subject_id == subject.pk for row in topics))
                self.assertContains(response, subject.name_kk)


class HomeSubjectsTests(TestCase):
    def test_home_lists_subjects_with_duration(self):
        run_command("load_subjects")
        response = self.client.get(reverse("quiz:home"))
        self.assertContains(response, "ПББ тестіне дайындық")
        self.assertEqual(len(response.context["subjects"]), 4)
        self.assertContains(response, "80 минут")
        self.assertContains(response, "125 минут")


# ---------- 12-кезең: формулалар, жауаптағы суреттер, әр пәнге демо ----------

# Сурет жүктейтін тесттер файлдарды уақытша папкаға жазады (media/ ластанбайды)
TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="ozp-test-media-")
atexit.register(shutil.rmtree, TEST_MEDIA_ROOT, ignore_errors=True)


def png_file(name="answer.png", big=False):
    """Жүктеуге арналған PNG сурет; big=True — 2 МБ-тан үлкен (кездейсоқ нүктелер, сығылмайды)."""
    buffer = BytesIO()
    if big:
        side = 1100
        Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(buffer, "PNG")
    else:
        Image.new("RGB", (40, 30), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class AnswerImageFormTests(TeacherTestCase):
    def answer_data(self, **overrides):
        data = self.question_data()
        data.update(overrides)
        return data

    def post(self, data):
        return self.client.post(reverse("quiz:teacher_question_create"), data)

    def test_image_only_answer_is_accepted(self):
        data = self.answer_data(**{"answers-1-text": ""})
        data["answers-1-image"] = png_file("correct-answer.png")
        response = self.post(data)
        self.assertEqual(response.status_code, 302)
        answer = Answer.objects.get(text="")
        self.assertTrue(answer.image)
        # Файл аты кездейсоқ: бастапқы атауынан ештеңе қалмайды
        self.assertTrue(answer.image.name.startswith("answers/"))
        self.assertNotIn("correct", answer.image.name)
        self.assertRegex(answer.image.name, r"^answers/[0-9a-f]{32}\.png$")

    def test_answer_without_text_and_image_is_rejected(self):
        response = self.post(self.answer_data(**{"answers-2-text": "   "}))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Әр жауап нұсқасында мәтін немесе сурет болуы керек.",
            str(response.context["formset"].forms[2].non_field_errors()),
        )
        self.assertFalse(Question.objects.exists())

    def test_text_and_image_together(self):
        data = self.answer_data()
        data["answers-0-image"] = png_file()
        self.assertEqual(self.post(data).status_code, 302)
        answer = Answer.objects.get(text="ENIAC")
        self.assertTrue(answer.image)

    def test_large_answer_image_is_rejected(self):
        image = png_file("big.png", big=True)
        self.assertGreater(image.size, MAX_IMAGE_MB * 1024 * 1024)
        data = self.answer_data()
        data["answers-3-image"] = image
        response = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("2 МБ", str(response.context["formset"].forms[3].errors["image"]))
        self.assertFalse(Question.objects.exists())

    def test_wrong_format_is_rejected(self):
        gif = BytesIO()
        Image.new("RGB", (10, 10), "red").save(gif, format="GIF")
        data = self.answer_data(**{"answers-3-text": ""})
        data["answers-3-image"] = SimpleUploadedFile("a.gif", gif.getvalue(), "image/gif")
        response = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("image", response.context["formset"].forms[3].errors)
        self.assertFalse(Question.objects.exists())

    def test_question_image_size_limit(self):
        data = self.answer_data()
        data["image"] = png_file("question.png", big=True)
        response = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("image", response.context["form"].errors)

    def test_form_shows_image_inputs(self):
        response = self.client.get(reverse("quiz:teacher_question_create"))
        self.assertContains(response, 'name="answers-0-image"')
        self.assertContains(response, 'accept="image/jpeg,image/png,image/webp"')

    def test_admin_inline_requires_text_or_image(self):
        formset_class = inlineformset_factory(
            Question,
            Answer,
            formset=AnswerInlineFormSet,
            fields=["text", "image", "is_correct"],
            extra=ANSWERS_PER_QUESTION,
        )
        question = self.create_question()
        question.answers.all().delete()
        data = {"answers-TOTAL_FORMS": "4", "answers-INITIAL_FORMS": "0"}
        for index in range(3):
            data[f"answers-{index}-text"] = str(index)
        data["answers-0-is_correct"] = "on"
        data["answers-3-is_correct"] = ""
        data["answers-3-text"] = ""
        # 4-жол: «дұрыс» белгісі жоқ, мәтін де, сурет те жоқ — бос жол (саналмайды)
        formset = formset_class(data, instance=question, prefix="answers")
        self.assertFalse(formset.is_valid())
        files = {"answers-3-image": png_file()}
        formset = formset_class(data, files, instance=question, prefix="answers")
        self.assertTrue(formset.is_valid(), formset.non_form_errors())


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class AnswerImageDisplayTests(TakeTestCase):
    def test_answer_image_is_shown_with_limited_height(self):
        attempt = self.new_attempt()
        item = attempt.items.get(order=1)
        answer = Answer.objects.get(pk=item.answer_order[0])
        answer.image.save("x.png", png_file(), save=True)
        response = self.client.get(self.question_url(attempt, 1))
        self.assertContains(response, f'src="{answer.image.url}" class="answer-image"')
        # Басқанда толық өлшемде ашылады
        self.assertContains(response, f'<a href="{answer.image.url}" target="_blank"')


class FormulaTests(StudentSubjectTestCase):
    """KaTeX тек формуласы бар пәннің (математика) сұрақ беттерінде қосылады."""

    def setUp(self):
        super().setUp()
        self.subtopic = Subtopic.objects.get(topic__subject=self.mathematics, number=1)
        self.question = Question.objects.create(
            subtopic=self.subtopic, language="kk", text="Есептеңіз: \\(\\frac{1}{2} < a\\)", level="A"
        )
        for index, text in enumerate(["\\(\\sqrt{x}\\)", "2", "3", "4"]):
            Answer.objects.create(question=self.question, text=text, is_correct=index == 0)

    def make_attempt(self, student, session):
        attempt = Attempt.objects.create(
            user=student, session=session, language="kk", deadline=session.closes_at
        )
        AttemptQuestion.objects.create(
            attempt=attempt,
            question=self.question,
            order=1,
            answer_order=list(self.question.answers.values_list("id", flat=True)),
        )
        return attempt

    def test_math_question_page_loads_katex_and_keeps_text(self):
        session = self.session_for(self.mathematics)
        attempt = self.make_attempt(self.student, session)
        response = self.client.get(reverse("quiz:attempt_question", args=[attempt.pk, 1]))
        self.assertContains(response, "katex.min.js")
        self.assertContains(response, "js/formulas.js")
        # Мәтін өзгеріссіз, autoescape сақталады
        self.assertContains(response, "Есептеңіз: \\(\\frac{1}{2} &lt; a\\)")
        self.assertContains(response, "\\(\\sqrt{x}\\)")

    def test_informatics_pages_do_not_load_katex(self):
        inf_student = make_student(username="inf", group=self.inf_group)
        inf_question = Question.objects.create(
            subtopic=Subtopic.objects.get(topic__subject=self.informatics, number=1),
            language="kk",
            text="Мәтін \\(x\\)",
            level="A",
        )
        for index in range(ANSWERS_PER_QUESTION):
            Answer.objects.create(question=inf_question, text=str(index), is_correct=index == 0)
        session = self.session_for(self.informatics)
        attempt = Attempt.objects.create(
            user=inf_student, session=session, language="kk", deadline=session.closes_at
        )
        AttemptQuestion.objects.create(
            attempt=attempt,
            question=inf_question,
            order=1,
            answer_order=list(inf_question.answers.values_list("id", flat=True)),
        )
        self.client.force_login(inf_student)
        response = self.client.get(reverse("quiz:attempt_question", args=[attempt.pk, 1]))
        self.assertNotContains(response, "katex")
        self.assertContains(response, "Мәтін \\(x\\)")

    def test_result_and_practice_load_katex(self):
        session = self.session_for(self.mathematics)
        attempt = self.make_attempt(self.student, session)
        finish_attempt(attempt)
        response = self.client.get(reverse("quiz:attempt_result", args=[attempt.pk]))
        self.assertContains(response, "katex.min.js")

        self.client.post(
            reverse("quiz:practice_start"), {"topic": self.subtopic.topic_id, "language": "kk"}
        )
        response = self.client.get(reverse("quiz:practice_question", args=[1]))
        self.assertContains(response, "katex.min.js")

    def test_teacher_form_shows_formula_hint_only_for_math(self):
        teacher = make_teacher(username="math", subjects=[self.mathematics])
        self.client.force_login(teacher)
        response = self.client.get(
            reverse("quiz:teacher_question_edit", args=[self.question.pk])
        )
        self.assertContains(response, "<code>\\(\\frac{a}{b}\\)</code>")
        self.assertContains(response, "katex.min.js")

        teacher = make_teacher(username="inf", subjects=[self.informatics])
        self.client.force_login(teacher)
        response = self.client.get(reverse("quiz:teacher_question_create"))
        self.assertNotContains(response, "\\(\\frac{a}{b}\\)")
        self.assertNotContains(response, "katex")


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class DemoAllSubjectsTests(TestCase):
    """Әр пәнге демо деректер: 4 пәннің әрқайсында толық нұсқа құрылады (TZ.md, 10.9)."""

    @classmethod
    def setUpTestData(cls):
        run_command("load_subjects")
        run_command("load_demo")

    def test_variant_for_every_subject_and_language(self):
        for subject in Subject.objects.all():
            for language in ["kk", "ru"]:
                with self.subTest(subject=subject.code, language=language):
                    ids = build_variant(subject, language, random.Random(1))
                    questions = list(
                        Question.objects.filter(pk__in=ids).select_related("subtopic__topic")
                    )
                    self.assertEqual(len(questions), QUESTIONS_TOTAL)
                    self.assertEqual(
                        {question.subtopic.topic.subject_id for question in questions},
                        {subject.pk},
                    )
                    self.assertEqual({question.language for question in questions}, {language})
                    self.assertEqual(Counter(q.level for q in questions), Counter(LEVEL_QUOTA))
                    singles = Counter(
                        q.subtopic.number for q in questions if q.context_id is None
                    )
                    self.assertEqual(set(singles.values()), {SINGLE_QUESTIONS_PER_SUBTOPIC})
                    self.assertEqual(len(singles), SUBTOPICS_COUNT)
                    contexts = Counter(q.context_id for q in questions if q.context_id)
                    self.assertEqual(sorted(contexts.values()), [QUESTIONS_PER_CONTEXT] * 2)

    def test_demo_contexts_belong_to_subject(self):
        for context in Context.objects.prefetch_related("questions__subtopic__topic"):
            for question in context.questions.all():
                self.assertEqual(question.subtopic.topic.subject_id, context.subject_id)

    def test_math_demo_has_formulas_and_image_answers(self):
        math_questions = Question.objects.filter(subtopic__topic__subject__code="mathematics")
        self.assertTrue(math_questions.filter(text__contains="\\(").exists())
        image_question = math_questions.get(language="kk", text="Суреттегі қай фигура — ромб?")
        answers = list(image_question.answers.all())
        self.assertEqual(len(answers), ANSWERS_PER_QUESTION)
        self.assertTrue(all(answer.image and not answer.text for answer in answers))
        self.assertEqual(sum(answer.is_correct for answer in answers), 1)
        self.assertNotIn("rhombus", "".join(answer.image.name for answer in answers))
        self.assertTrue(Context.objects.filter(text__contains="\\(f(x)").exists())

    def test_second_run_does_not_duplicate(self):
        count = Question.objects.count()
        run_command("load_demo")
        self.assertEqual(Question.objects.count(), count)

    def test_delete_one_subject(self):
        image_names = list(
            Answer.objects.filter(question__subtopic__topic__subject__code="mathematics")
            .exclude(image="")
            .values_list("image", flat=True)
        )
        run_command("load_demo", "--delete", "--subject", "mathematics")
        self.assertFalse(
            Question.objects.filter(subtopic__topic__subject__code="mathematics").exists()
        )
        self.assertFalse(Context.objects.filter(subject__code="mathematics").exists())
        self.assertTrue(
            Question.objects.filter(subtopic__topic__subject__code="informatics").exists()
        )
        # Демо суреттердің файлдары да өшті
        self.assertTrue(image_names)
        self.assertFalse(any(default_storage.exists(name) for name in image_names))


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class DemoOneSubjectTests(TestCase):
    def test_load_one_subject(self):
        run_command("load_subjects")
        run_command("load_demo", "--subject", "art_labor_girls")
        subjects = set(
            Question.objects.values_list("subtopic__topic__subject__code", flat=True)
        )
        self.assertEqual(subjects, {"art_labor_girls"})

    def test_subject_without_topics(self):
        with self.assertRaises(CommandError):
            run_command("load_demo", "--subject", "informatics")
        with self.assertRaises(CommandError):
            run_command("load_demo", "--subject", "physics")


class LoadtestDataSubjectTests(TestCase):
    def test_subject_option(self):
        run_command("load_subjects")
        run_command("loadtest_data", "--count", "2", "--subject", "mathematics")
        group = StudyGroup.objects.get(name="LOADTEST")
        session = ExamSession.objects.get(title=LOADTEST_SESSION_TITLE)
        self.assertEqual(group.subject.code, "mathematics")
        self.assertEqual(session.subject.code, "mathematics")
        student = User.objects.get(username="student001")
        self.assertEqual(list(visible_sessions(student)), [session])

        # Қайта іске қосқанда пән ауысады
        run_command("loadtest_data", "--count", "2")
        group.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(group.subject.code, "informatics")
        self.assertEqual(session.subject.code, "informatics")

    def test_unknown_subject(self):
        with self.assertRaises(CommandError):
            run_command("loadtest_data", "--subject", "physics")


class FormulaScriptTests(TestCase):
    def test_delimiters_are_escaped_in_js(self):
        # JS-те "\\(" деп жазылуы керек (= \( ). "\(" деп жазылса, JS оны жай "("
        # деп оқиды да, кез келген жақшадағы мәтін формула болып көрсетіледі
        script = (settings.BASE_DIR / "static" / "js" / "formulas.js").read_text(encoding="utf-8")
        for delimiter in [r'"\\("', r'"\\)"', r'"\\["', r'"\\]"']:
            self.assertIn(delimiter, script)
        self.assertNotIn(r'"\("', script)


class DashboardCarouselTests(StudentTestCase):
    """Кабинет: әр тізімде слайдқа 2 сессиядан, ашық сессиялардың тапсырылмағаны бірінші."""

    def test_sessions_are_split_into_slides_of_two(self):
        for index in range(5):
            self.session_for(self.group, title=f"Ашық {index}")
        lists = dashboard_sessions(self.student)
        self.assertEqual([len(slide) for slide in lists["open_slides"]], [2, 2, 1])
        self.assertEqual(lists["upcoming_slides"], [])

        response = self.client.get(reverse("quiz:dashboard"))
        # Барлық сессия бетте бар (басқа слайдтарда), ауыстырғыш шығады
        for index in range(5):
            self.assertContains(response, f"Ашық {index}")
        self.assertContains(response, 'data-bs-target="#open-sessions" data-bs-slide="next"')
        self.assertContains(response, 'data-bs-slide-to="2"')

    def test_no_controls_for_two_sessions(self):
        self.session_for(self.group, title="Бірінші")
        self.session_for(self.group, title="Екінші")
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, "Екінші")
        self.assertNotContains(response, "data-bs-slide=")

    def test_empty_list_text(self):
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, "Қазір ашық сессия жоқ.")
        self.assertNotContains(response, 'class="carousel slide')

    def test_unfinished_open_sessions_come_first(self):
        done_1 = self.session_for(self.group, title="Тапсырылған 1")
        done_2 = self.session_for(self.group, title="Тапсырылған 2")
        todo = self.session_for(self.group, title="Тапсыру керек")
        for session in [done_1, done_2]:
            Attempt.objects.create(
                user=self.student,
                session=session,
                language="kk",
                deadline=session.closes_at,
                status=Attempt.Status.FINISHED,
                score=10,
            )
        first_slide = dashboard_sessions(self.student)["open_slides"][0]
        self.assertEqual(first_slide[0]["session"], todo)


# ---------- Тест беті AJAX-пен және тест кезінде шығуға тыйым ----------


class AjaxAttemptTests(TakeTestCase):
    """Тест беті бетті қайта жүктемей жұмыс істейді: бір бетте 50 сұрақ, жауап — JSON."""

    AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def setUp(self):
        super().setUp()
        self.attempt = self.new_attempt()

    def ajax_answer(self, number, answer_id):
        return self.client.post(
            self.question_url(self.attempt, number), {"answer": answer_id}, **self.AJAX
        )

    def test_page_holds_all_questions_one_visible(self):
        response = self.client.get(self.question_url(self.attempt, 7))
        content = response.content.decode()
        self.assertEqual(content.count('class="question-block'), QUESTIONS_TOTAL)
        # Тек 7-сұрақ көрінеді, қалғаны жасырын
        self.assertEqual(content.count('class="question-block"'), 1)
        self.assertIn('id="question-7" class="question-block"', content)
        self.assertContains(response, "js/attempt.js")
        self.assertContains(response, 'data-current="7"')

    def test_ajax_answer_returns_json(self):
        item = self.attempt.items.get(order=5)
        response = self.ajax_answer(5, self.wrong_answer(item).pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"saved": True, "unanswered": QUESTIONS_TOTAL - 1})
        item.refresh_from_db()
        self.assertEqual(item.selected, self.wrong_answer(item))

        # Жауапты өзгерту — сан өзгермейді
        response = self.ajax_answer(5, self.correct_answer(item).pk)
        self.assertEqual(response.json()["unanswered"], QUESTIONS_TOTAL - 1)
        item.refresh_from_db()
        self.assertEqual(item.selected, self.correct_answer(item))

    def test_ajax_response_has_no_correct_answer(self):
        item = self.attempt.items.get(order=2)
        response = self.ajax_answer(2, self.wrong_answer(item).pk)
        self.assertEqual(set(response.json()), {"saved", "unanswered"})

    def test_ajax_invalid_answer(self):
        other = self.attempt.items.get(order=2)
        response = self.ajax_answer(1, self.correct_answer(other).pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertIsNone(self.attempt.items.get(order=1).selected)

    def test_ajax_after_deadline_redirects_to_result(self):
        self.expire(self.attempt)
        item = self.attempt.items.get(order=1)
        response = self.ajax_answer(1, self.correct_answer(item).pk)
        result_url = reverse("quiz:attempt_result", args=[self.attempt.pk])
        self.assertEqual(response.json(), {"redirect": result_url})
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, Attempt.Status.FINISHED)
        item.refresh_from_db()
        self.assertIsNone(item.selected)

    def test_ajax_finished_attempt_redirects(self):
        finish_attempt(self.attempt)
        item = self.attempt.items.get(order=1)
        response = self.ajax_answer(1, self.correct_answer(item).pk)
        self.assertEqual(
            response.json(), {"redirect": reverse("quiz:attempt_result", args=[self.attempt.pk])}
        )

    def test_foreign_attempt_is_404_for_ajax(self):
        other = User.objects.create_user(username="other", password="pass12345")
        self.client.force_login(other)
        item = self.attempt.items.get(order=1)
        self.assertEqual(self.ajax_answer(1, self.correct_answer(item).pk).status_code, 404)


class LogoutDuringAttemptTests(TakeTestCase):
    """Тест жүріп жатқанда аккаунттан шығуға болмайды."""

    def test_logout_is_blocked_while_attempt_in_progress(self):
        attempt = self.new_attempt()
        response = self.client.post(reverse("accounts:logout"))
        # Хабарлама оқылмай тұрсын (assertRedirects бетті өзі ашып қояды)
        self.assertRedirects(
            response, self.question_url(attempt, 1), fetch_redirect_response=False
        )
        self.assertIn("_auth_user_id", self.client.session)
        response = self.client.get(self.question_url(attempt, 1))
        self.assertContains(response, "Тест аяқталмай тұрып аккаунттан шығуға болмайды")

    def test_navbar_hides_logout_form_during_attempt(self):
        attempt = self.new_attempt()
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertNotContains(response, f'action="{reverse("accounts:logout")}"')
        self.assertContains(response, "Шығу үшін алдымен тестті аяқтаңыз")
        # Басқа беттен тестке оралу сілтемесі
        self.assertContains(response, self.question_url(attempt, 1))

    def test_logout_after_finish(self):
        attempt = self.new_attempt()
        finish_attempt(attempt)
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, f'action="{reverse("accounts:logout")}"')
        response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("quiz:home"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_after_deadline(self):
        attempt = self.new_attempt()
        self.expire(attempt)
        response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("quiz:home"))

    def test_logout_requires_post(self):
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)


class AjaxPracticeTests(VariantTestCase):
    """Жаттығу беті бетті қайта жүктемей: бір бетте барлық сұрақ, жауап — JSON."""

    AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def setUp(self):
        self.student = make_student()
        self.client.force_login(self.student)
        self.client.post(
            reverse("quiz:practice_start"),
            {"topic": Topic.objects.get(number=8).pk, "language": "kk"},
        )

    def question(self, number):
        item = self.client.session["practice"]["items"][number - 1]
        return Question.objects.get(pk=item["question"])

    def ajax_answer(self, number, answer_id):
        return self.client.post(
            reverse("quiz:practice_question", args=[number]), {"answer": answer_id}, **self.AJAX
        )

    def test_page_holds_all_questions_one_visible(self):
        response = self.client.get(reverse("quiz:practice_question", args=[3]))
        content = response.content.decode()
        self.assertEqual(content.count('class="question-block'), PRACTICE_QUESTIONS)
        self.assertEqual(content.count('class="question-block"'), 1)
        self.assertIn('id="question-3" class="question-block"', content)
        self.assertContains(response, "js/question_pages.js")
        self.assertContains(response, "js/practice.js")

    def test_correct_answer_feedback_json(self):
        correct = self.question(1).answers.get(is_correct=True)
        response = self.ajax_answer(1, correct.pk)
        self.assertEqual(
            response.json(),
            {"selected_id": correct.pk, "correct_id": correct.pk, "is_correct": True},
        )
        self.assertEqual(self.client.session["practice"]["items"][0]["selected"], correct.pk)

    def test_wrong_answer_feedback_json_and_cannot_change(self):
        question = self.question(2)
        wrong = question.answers.filter(is_correct=False).first()
        correct = question.answers.get(is_correct=True)
        response = self.ajax_answer(2, wrong.pk)
        self.assertEqual(
            response.json(),
            {"selected_id": wrong.pk, "correct_id": correct.pk, "is_correct": False},
        )
        # Екінші жауап қабылданбайды — бұрынғы нәтиже қайтады
        response = self.ajax_answer(2, correct.pk)
        self.assertEqual(response.json()["selected_id"], wrong.pk)
        self.assertFalse(response.json()["is_correct"])

    def test_ajax_invalid_answer(self):
        other = self.question(2).answers.first()
        response = self.ajax_answer(1, other.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertIsNone(self.client.session["practice"]["items"][0]["selected"])

    def test_ajax_without_practice_redirects_to_start(self):
        session = self.client.session
        del session["practice"]
        session.save()
        response = self.ajax_answer(1, 1)
        self.assertEqual(response.json(), {"redirect": reverse("quiz:practice_start")})

    def test_ajax_deleted_answers_restart_practice(self):
        question = self.question(1)
        answer_id = question.answers.first().pk
        # Жаттығу кезінде сұрақтың жауап нұсқалары банктен өшірілді
        question.answers.all().delete()
        response = self.ajax_answer(1, answer_id)
        self.assertEqual(response.json(), {"redirect": reverse("quiz:practice_start")})
        self.assertNotIn("practice", self.client.session)


class GroupLanguageTests(StudentSubjectTestCase):
    """Тест тілі — топтың оқыту тілі: топ құрғанда көрсетіледі, студент таңдамайды."""

    def test_dashboard_shows_test_language(self):
        response = self.client.get(reverse("quiz:dashboard"))
        self.assertContains(response, "Тест тілі: Қазақша")

    def test_practice_defaults_to_group_language(self):
        self.math_group.language = "ru"
        self.math_group.save()
        response = self.client.get(reverse("quiz:practice_start"))
        self.assertEqual(response.context["form"].initial["language"], "ru")

    def test_student_language_service(self):
        self.assertEqual(student_language(self.student), "kk")
        self.student.profile.group = None
        self.student.profile.save()
        self.assertIsNone(student_language(self.student))


# ---------- Сұрақтарды Excel-ден импорттау ----------


def xlsx_file(questions=(), contexts=None, name="questions.xlsx", images=()):
    """
    Импорт файлы: «Сұрақтар» парағы (және қажет болса «Контексттер»), 1-жол — тақырыптар.
    images — [(парақ атауы, ұяшық, сурет байттары), ...]: сурет ұяшықтың үстіне қойылады.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = QUESTIONS_SHEET
    sheet.append(["header"] * 12)
    for row in questions:
        sheet.append(list(row))
    if contexts is not None:
        context_sheet = workbook.create_sheet(CONTEXTS_SHEET)
        context_sheet.append(["header"] * 6)
        for row in contexts:
            context_sheet.append(list(row))
    for sheet_title, cell, data in images:
        image = ExcelImage(BytesIO(data))
        image.anchor = cell
        workbook[sheet_title].add_image(image)
    output = BytesIO()
    workbook.save(output)
    return SimpleUploadedFile(name, output.getvalue())


def question_row(subtopic="03", level="B", language="kk", context="", text="ЭЕМ?", **extra):
    """«Сұрақтар» парағының бір жолы (әдепкіде дұрыс толтырылған, дұрыс жауап — A)."""
    row = [subtopic, level, language, context, text, extra.get("code", "")]
    row += extra.get("answers", ["ENIAC", "IBM PC", "Apple I", "Altair"])
    row.append(extra.get("correct", "A"))
    return row


class QuestionImportTests(TeacherTestCase):
    def upload(self, file):
        return self.client.post(reverse("quiz:teacher_question_import"), {"file": file})

    def messages(self, response):
        return [str(message) for message in response.wsgi_request._messages]

    def test_page_opens_from_question_list(self):
        response = self.client.get(reverse("quiz:teacher_questions"))
        self.assertContains(response, reverse("quiz:teacher_question_import"))
        response = self.client.get(reverse("quiz:teacher_question_import"))
        self.assertContains(response, reverse("quiz:teacher_question_import_template"))

    def test_only_teacher_can_open(self):
        self.client.force_login(make_student())
        for url_name in ["teacher_question_import", "teacher_question_import_template"]:
            with self.subTest(url_name=url_name):
                self.assertEqual(self.client.get(reverse(f"quiz:{url_name}")).status_code, 302)

    def test_template_file(self):
        response = self.client.get(reverse("quiz:teacher_question_import_template"))
        self.assertEqual(response["Content-Type"], XLSX_CONTENT_TYPE)
        self.assertIn("questions-informatics.xlsx", response["Content-Disposition"])
        workbook = load_workbook(BytesIO(response.content))
        self.assertEqual(workbook.sheetnames[:2], [QUESTIONS_SHEET, CONTEXTS_SHEET])
        self.assertEqual(workbook[QUESTIONS_SHEET].max_column, 12)
        self.assertEqual(workbook[QUESTIONS_SHEET]["L1"].value, "Сурет")
        self.assertEqual(workbook[CONTEXTS_SHEET].max_column, 6)
        # Нұсқаулықта пәннің 20 тақырыпшасы
        guide = [row[0] for row in workbook.worksheets[2].iter_rows(values_only=True)]
        self.assertIn("01", guide)
        self.assertIn("20", guide)

    def test_import_single_and_context_questions(self):
        code = "for i in range(3):\n    print(i)"
        rows = [
            question_row(text="Бірінші", code=code, correct="C"),
            question_row(subtopic=5, level="A", context="K1", text="Кесте сұрағы"),
        ]
        contexts = [["K1", "Кесте", "kk", "Мәтін", ""]]
        response = self.upload(xlsx_file(rows, contexts))
        self.assertRedirects(response, reverse("quiz:teacher_questions"))

        first = Question.objects.get(text="Бірінші")
        self.assertEqual(first.subtopic, self.subtopic)
        self.assertEqual((first.level, first.language, first.code), ("B", "kk", code))
        self.assertIsNone(first.context)
        self.assertFalse(first.is_demo)
        self.assertEqual(
            [(answer.text, answer.is_correct) for answer in first.answers.all()],
            [("ENIAC", False), ("IBM PC", False), ("Apple I", True), ("Altair", False)],
        )
        second = Question.objects.get(text="Кесте сұрағы")
        self.assertEqual(second.subtopic.number, 5)
        self.assertEqual(second.context.title, "Кесте")
        self.assertEqual(second.context.subject, informatics())
        self.assertIn("Қосылды: сұрақтар — 2, контексттер — 1.", self.messages(response))

    def test_cyrillic_letters_and_language_names(self):
        # Кириллицаның А, В, С, Д әріптері латынша сияқты көрінеді
        self.upload(xlsx_file([question_row(level="В", language="орыс", correct="Д")]))
        question = Question.objects.get()
        self.assertEqual((question.level, question.language), ("B", "ru"))
        self.assertTrue(question.answers.get(text="Altair").is_correct)

    def test_errors_save_nothing(self):
        rows = [
            question_row(text="Дұрыс сұрақ"),
            question_row(level="D"),
            question_row(subtopic=21),
            question_row(language="en", text=""),
            question_row(answers=["1", "", "3", "4"], correct="E"),
        ]
        response = self.upload(xlsx_file(rows))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Question.objects.exists())
        errors = response.context["result"]["errors"]
        self.assertEqual(len(errors), 6)
        self.assertIn("«Сұрақтар» парағы, 3-жол, «Деңгей (A/B/C)»", errors[0])
        self.assertIn("4-жол, «Тақырыпша (01–20)»", errors[1])
        self.assertContains(response, "ештеңе сақталмады")

    def test_context_errors(self):
        rows = [
            question_row(context="K9"),
            question_row(language="ru", context="K1", text="Тілі басқа"),
        ]
        contexts = [["K1", "Кесте", "kk", "Мәтін", ""], ["K1", "Қайталанған", "kk", "Мәтін", ""]]
        errors = self.upload(xlsx_file(rows, contexts)).context["result"]["errors"]
        self.assertTrue(any("«K1» кілті 2-жолда бар" in error for error in errors))
        self.assertTrue(any("«K9» кілті «Контексттер» парағында жоқ" in error for error in errors))
        self.assertTrue(any("тілі бірдей болуы керек" in error for error in errors))
        self.assertFalse(Context.objects.exists())

    def test_context_cannot_have_more_than_5_questions(self):
        rows = [question_row(context="K1", text=f"Сұрақ {index}") for index in range(6)]
        response = self.upload(xlsx_file(rows, [["K1", "Кесте", "kk", "Мәтін", ""]]))
        errors = response.context["result"]["errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("6 сұрақ байланады, ең көбі 5", errors[0])
        self.assertFalse(Question.objects.exists())

    def test_duplicates_are_skipped(self):
        self.create_question(text="Бұрынғы", language="kk")
        rows = [
            question_row(text="Бұрынғы"),
            question_row(text="Жаңа"),
            question_row(text="Жаңа"),
        ]
        response = self.upload(xlsx_file(rows))
        self.assertEqual(Question.objects.filter(text="Жаңа").count(), 1)
        self.assertEqual(Question.objects.filter(text="Бұрынғы").count(), 1)
        self.assertIn(
            "Қосылды: сұрақтар — 1, контексттер — 0. "
            "Банкте бұрыннан бар, өткізілді — жолдар: 2, 4.",
            self.messages(response),
        )

    def test_reimport_reuses_existing_context(self):
        contexts = [["K1", "Кесте", "kk", "Мәтін", ""]]
        self.upload(xlsx_file([question_row(context="K1", text="Бірінші")], contexts))
        self.upload(xlsx_file([question_row(context="K1", text="Екінші")], contexts))
        quiz_context = Context.objects.get()
        self.assertEqual(quiz_context.questions.count(), 2)

    def test_questions_go_to_selected_subject(self):
        run_command("load_subjects", "--only", "mathematics")
        mathematics = Subject.objects.get(code="mathematics")
        self.teacher.profile.subjects.add(mathematics)
        session = self.client.session
        session["teacher_subject_id"] = mathematics.pk
        session.save()
        self.upload(xlsx_file([question_row(subtopic=3)]))
        question = Question.objects.get()
        self.assertEqual(question.subtopic.topic.subject, mathematics)

    def test_file_checks(self):
        response = self.upload(SimpleUploadedFile("questions.csv", b"a;b"))
        self.assertIn("file", response.context["form"].errors)
        response = self.upload(SimpleUploadedFile("questions.xlsx", b"not a zip"))
        self.assertIn("Файл оқылмады", response.context["result"]["errors"][0])
        response = self.upload(xlsx_file([]))
        self.assertEqual(response.context["result"]["errors"], ["Файлда сұрақ жоқ."])
        self.assertFalse(Question.objects.exists())

    @mock.patch("apps.quiz.question_import.IMPORT_MAX_ROWS", 2)
    def test_too_many_rows(self):
        rows = [question_row(text=f"Сұрақ {index}") for index in range(3)]
        response = self.upload(xlsx_file(rows))
        self.assertIn("Файлда жол тым көп", response.context["result"]["errors"][0])
        self.assertFalse(Question.objects.exists())

    def test_first_sheet_is_used_without_questions_sheet(self):
        workbook = Workbook()
        workbook.active.title = "Лист1"
        workbook.active.append(["header"] * 11)
        workbook.active.append(question_row())
        output = BytesIO()
        workbook.save(output)
        self.upload(SimpleUploadedFile("my.xlsx", output.getvalue()))
        self.assertEqual(Question.objects.count(), 1)

    def test_answer_needs_text_or_image(self):
        response = self.upload(xlsx_file([question_row(answers=["1", "", "3", "4"])]))
        errors = response.context["result"]["errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("2-жол, «B»: жауап нұсқасында мәтін не сурет болуы керек.", errors[0])

    def test_page_in_russian(self):
        self.client.cookies["django_language"] = "ru"

        def text_without_sheet_names(response):
            # Тек парақ атаулары қазақша қалады — импорт оларды осы атаумен іздейді
            text = visible_text(response)
            return text.replace(QUESTIONS_SHEET, "").replace(CONTEXTS_SHEET, "")

        response = self.client.get(reverse("quiz:teacher_question_import"))
        self.assertNotRegex(text_without_sheet_names(response), KAZAKH_ONLY_LETTERS)
        response = self.upload(xlsx_file([question_row(level="D")]))
        text = text_without_sheet_names(response)
        self.assertNotRegex(text, KAZAKH_ONLY_LETTERS)
        self.assertIn("строка 2", text)


def image_bytes(image_format="PNG", color="red"):
    """Excel-ге қойылатын шағын сурет (байттар)."""
    buffer = BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, format=image_format)
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class QuestionImportImageTests(TeacherTestCase):
    """Суреттер Excel ішінен импортталады: сұрақ, жауап және контекст суреті."""

    def upload(self, file):
        return self.client.post(reverse("quiz:teacher_question_import"), {"file": file})

    def errors(self, response):
        return response.context["result"]["errors"]

    def test_question_answer_and_context_images(self):
        question_png = image_bytes(color="red")
        rows = [question_row(context="K1", answers=["ENIAC", "", "Apple I", "Altair"])]
        contexts = [["K1", "Кесте", "kk", "Мәтін", ""]]
        images = [
            (QUESTIONS_SHEET, "L2", question_png),  # «Сурет» бағаны — сұрақ суреті
            (QUESTIONS_SHEET, "H2", image_bytes("JPEG", color="blue")),  # B жауабы — тек сурет
            (CONTEXTS_SHEET, "F2", image_bytes("WEBP", color="green")),
        ]
        response = self.upload(xlsx_file(rows, contexts, images=images))
        self.assertRedirects(response, reverse("quiz:teacher_questions"))

        question = Question.objects.get()
        self.assertTrue(question.image.name.startswith("questions/"))
        self.assertTrue(question.image.name.endswith(".png"))
        with question.image.open("rb") as file:
            self.assertEqual(file.read(), question_png)
        answer = question.answers.get(text="")
        self.assertTrue(answer.image.name.startswith("answers/"))
        self.assertTrue(answer.image.name.endswith(".jpg"))
        self.assertFalse(answer.is_correct)
        self.assertFalse(question.answers.exclude(pk=answer.pk).exclude(image="").exists())
        # webp PNG-ге айналады
        self.assertTrue(question.context.image.name.startswith("contexts/"))
        self.assertTrue(question.context.image.name.endswith(".png"))

    def test_image_errors_save_nothing(self):
        rows = [question_row(text=f"Сұрақ {index}") for index in range(4)]
        images = [
            (QUESTIONS_SHEET, "E2", image_bytes()),  # мәтін бағаны — болмайды
            (QUESTIONS_SHEET, "L3", image_bytes()),  # бір ұяшықта екі сурет
            (QUESTIONS_SHEET, "L3", image_bytes(color="blue")),
            (QUESTIONS_SHEET, "L4", image_bytes("GIF")),  # gif — болмайды
            (QUESTIONS_SHEET, "L9", image_bytes()),  # бос жол
        ]
        errors = self.errors(self.upload(xlsx_file(rows, images=images)))
        self.assertEqual(len(errors), 4)
        self.assertIn("2-жол, «Сұрақ мәтіні»: сурет мына бағандардың бірінде тұруы керек", errors[0])
        self.assertIn("3-жол, «Сурет»: бір ұяшықта бір ғана сурет болуы керек.", errors[1])
        self.assertIn("4-жол, «Сурет»: сурет пішімі jpg, png немесе webp болуы керек.", errors[2])
        self.assertIn("9-жол, «Сурет»: суреті бар жолда мәлімет жоқ.", errors[3])
        self.assertFalse(Question.objects.exists())

    @mock.patch("apps.quiz.question_import.MAX_IMAGE_MB", 0)
    def test_too_big_image(self):
        images = [(QUESTIONS_SHEET, "L2", image_bytes())]
        errors = self.errors(self.upload(xlsx_file([question_row()], images=images)))
        self.assertEqual(
            errors, ["«Сұрақтар» парағы, 2-жол, «Сурет»: сурет тым үлкен: ең көбі 0 МБ."]
        )
        self.assertFalse(Question.objects.exists())

    def test_image_in_cell_is_reported(self):
        # Excel 365-тің «ұяшықтың ішіне» қойылған суреті openpyxl-ге #VALUE! болып көрінеді
        errors = self.errors(self.upload(xlsx_file([question_row() + ["#VALUE!"]])))
        self.assertEqual(len(errors), 1)
        self.assertIn("сурет ұяшықтың ішіне қойылған", errors[0])

    def test_image_in_answer_cell_is_reported(self):
        # Жауап ұяшығындағы «ұяшықтың ішіне» қойылған сурет «#VALUE!» мәтіні болып сақталмауы керек
        answers = ["ENIAC", "#VALUE!", "Apple I", "Altair"]
        errors = self.errors(self.upload(xlsx_file([question_row(answers=answers)])))
        self.assertEqual(len(errors), 1)
        self.assertIn("2-жол, «B»: сурет ұяшықтың ішіне қойылған", errors[0])
        self.assertFalse(Question.objects.exists())
        self.assertFalse(Answer.objects.filter(text="#VALUE!").exists())

    def test_context_image_in_wrong_column(self):
        rows = [question_row(context="K1")]
        contexts = [["K1", "Кесте", "kk", "Мәтін", ""]]
        images = [(CONTEXTS_SHEET, "D2", image_bytes())]
        errors = self.errors(self.upload(xlsx_file(rows, contexts, images=images)))
        self.assertEqual(len(errors), 1)
        self.assertIn("«Контексттер» парағы, 2-жол, «Мәтіні»", errors[0])
        self.assertFalse(Context.objects.exists())
