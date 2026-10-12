"""
Бизнес-логика (TZ.md, 3–4-бөлімдер): сұрақты сақтау және көшіру, банк толуы,
студентке көрінетін сессиялар, тест нұсқасын құру, тест тапсыру (жауап сақтау,
аяқтау, балл есептеу), нәтиже талдауы және тақырыптық жаттығу.
"""

import csv
import logging
import math
import random
from collections import Counter
from datetime import timedelta
from itertools import combinations

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from .constants import (
    CONTEXTS_PER_TEST,
    LEVEL_QUOTA,
    MIN_CONTEXTS,
    MIN_QUESTIONS_PER_SUBTOPIC,
    PRACTICE_QUESTIONS,
    QUESTIONS_PER_CONTEXT,
    QUESTIONS_TOTAL,
    SINGLE_QUESTIONS_PER_SUBTOPIC,
)
from .models import (
    Answer,
    Attempt,
    AttemptQuestion,
    Context,
    ExamSession,
    Language,
    Level,
    Question,
    Subject,
    Subtopic,
    Topic,
)

logger = logging.getLogger(__name__)


# ---------- Пәндер ----------


def active_subjects():
    """Белсенді пәндер реті бойынша (басты беттегі тізім)."""
    return Subject.objects.filter(is_active=True)


def student_subject(user):
    """Студенттің пәні — тобының пәні (тобы жоқ болса — None)."""
    profile = getattr(user, "profile", None)
    if profile is None or profile.group is None:
        return None
    return profile.group.subject


def student_language(user):
    """Студенттің тест тілі — тобының оқыту тілі (тобы жоқ болса — None)."""
    profile = getattr(user, "profile", None)
    if profile is None or profile.group is None:
        return None
    return profile.group.language


def teacher_subjects(user):
    """
    Оқытушыға ашық белсенді пәндер: әкімшіге (is_superuser) — барлығы,
    оқытушыға — admin-де тағайындалғандары (Profile.subjects).
    """
    subjects = Subject.objects.filter(is_active=True)
    if user.is_superuser:
        return subjects
    return subjects.filter(teacher_profiles__user=user)


def choose_subject(subjects, subject_id):
    """
    Оқытушы беттерінің пәні: сессияда сақталған пән (subject_id) оқытушыға
    ашық болса — сол, әйтпесе бірінші пәні. Пәні жоқ болса — None.
    """
    for subject in subjects:
        if subject.pk == subject_id:
            return subject
    return subjects[0] if subjects else None


# ---------- Сұрақтар ----------


@transaction.atomic
def save_question(form, formset):
    """Сұрақты және оның 4 жауап нұсқасын бірге сақтайды (формалар тексерілген)."""
    question = form.save()
    formset.instance = question
    formset.save()
    return question


def context_has_room(context):
    """Контекстте жаңа белсенді сұраққа орын бар ма (5-тен аз)."""
    return context.active_questions_count() < QUESTIONS_PER_CONTEXT


@transaction.atomic
def copy_question(question):
    """
    Сұрақтың көшірмесін жауаптарымен бірге жасайды.
    Контекст толып қалса (5 сұрақ), көшірме жеке сұрақ болады.
    """
    context = question.context
    if context and not context_has_room(context):
        context = None

    copy = Question.objects.create(
        subtopic=question.subtopic,
        context=context,
        language=question.language,
        text=question.text,
        image=question.image,
        code=question.code,
        level=question.level,
        is_demo=question.is_demo,
    )
    for answer in question.answers.all():
        # Сурет файлы көшірілмейді: көшірме сол файлға сілтейді
        Answer.objects.create(
            question=copy, text=answer.text, image=answer.image, is_correct=answer.is_correct
        )
    return copy


def toggle_question_active(question):
    """
    Сұрақты «белсенді емес» етеді немесе қайта белсендіреді (сұрақ жойылмайды,
    сондықтан ескі нәтижелер бұзылмайды). Контекст толып қалса, сұрақ
    белсендірілмейді — онда False қайтарады.
    """
    if not question.is_active and question.context and not context_has_room(question.context):
        return False
    question.is_active = not question.is_active
    question.save(update_fields=["is_active"])
    return True


# ---------- Банк толуы ----------


def full_contexts(subject, language):
    """Тестке жарайтын контексттер: пәннің белсенді, дәл 5 белсенді сұрағы бар контексттері."""
    return (
        Context.objects.filter(subject=subject, is_active=True, language=language)
        .annotate(active_count=Count("questions", filter=Q(questions__is_active=True)))
        .filter(active_count=QUESTIONS_PER_CONTEXT)
    )


def bank_coverage(subject):
    """
    Пәннің банк толуы: әр тақырыпша × тіл × деңгей бойынша белсенді жеке сұрақтар саны.

    Жетіспейтін жерлер `missing=True` деп белгіленеді:
    - деңгей ұяшығы — сұрақ мүлде жоқ болса;
    - тақырыпша жиыны — MIN_QUESTIONS_PER_SUBTOPIC-тен (6) аз болса;
    - деңгей бойынша жалпы сан — LEVEL_QUOTA-дан (13/30/7) аз болса;
    - толық контексттер (5 белсенді сұрақ) — MIN_CONTEXTS-тен (4) аз болса.
    """
    languages = Language.values
    levels = Level.values

    # (тақырыпша id, тіл, деңгей) → сұрақ саны
    counts = {}
    grouped = (
        Question.objects.filter(
            subtopic__topic__subject=subject, is_active=True, context__isnull=True
        )
        .values("subtopic_id", "language", "level")
        .annotate(total=Count("id"))
    )
    for row in grouped:
        counts[(row["subtopic_id"], row["language"], row["level"])] = row["total"]

    # Кесте жолдары: ұяшықтар реті — kk: A, B, C, Σ, ru: A, B, C, Σ
    rows = []
    level_totals = {(language, level): 0 for language in languages for level in levels}
    for subtopic in Subtopic.objects.filter(topic__subject=subject).select_related("topic"):
        cells = []
        for language in languages:
            subtopic_total = 0
            for level in levels:
                count = counts.get((subtopic.pk, language, level), 0)
                cells.append({"count": count, "missing": count == 0, "is_total": False})
                subtopic_total += count
                level_totals[(language, level)] += count
            cells.append(
                {
                    "count": subtopic_total,
                    "missing": subtopic_total < MIN_QUESTIONS_PER_SUBTOPIC,
                    "is_total": True,
                }
            )
        rows.append({"subtopic": subtopic, "cells": cells})

    # Қорытынды жол: деңгей бойынша жалпы сан квотамен салыстырылады
    totals = []
    for language in languages:
        language_total = 0
        for level in levels:
            count = level_totals[(language, level)]
            totals.append(
                {"count": count, "missing": count < LEVEL_QUOTA[level], "is_total": False}
            )
            language_total += count
        totals.append(
            {
                "count": language_total,
                "missing": language_total < MIN_QUESTIONS_PER_SUBTOPIC * len(rows),
                "is_total": True,
            }
        )

    contexts = []
    for language, label in Language.choices:
        count = full_contexts(subject, language).count()
        contexts.append(
            {"label": label, "count": count, "missing": count < MIN_CONTEXTS}
        )

    return {
        "languages": Language.choices,
        "levels": levels,
        "rows": rows,
        "totals": totals,
        "contexts": contexts,
        "level_quota": LEVEL_QUOTA,
        "min_questions": MIN_QUESTIONS_PER_SUBTOPIC,
        "min_contexts": MIN_CONTEXTS,
        "questions_per_context": QUESTIONS_PER_CONTEXT,
    }


# ---------- Студентке арналған сессиялар ----------


def visible_sessions(user):
    """
    Студентке көрінетін белсенді сессиялар (TZ.md, 10.5): тобының пәніндегі
    сессиялар — оның тобына арналғандары және топтары бос (сол пәннің
    барлық тобына арналған) сессиялар. Тобы жоқ студент ештеңе көрмейді.
    """
    profile = getattr(user, "profile", None)
    group = profile.group if profile else None
    if group is None:
        return ExamSession.objects.none()

    for_group = Q(groups__isnull=True) | Q(groups=group)
    return (
        ExamSession.objects.filter(for_group, is_active=True, subject_id=group.subject_id)
        .select_related("subject")
        .distinct()
    )


def split_duration(duration):
    """
    Уақыт аралығын күн, сағат, минутқа бөледі (шаблонда аударылатын мәтінмен
    көрсету үшін; Django-ның timeuntil сүзгісінде қазақша аударма жоқ).
    """
    minutes_total = int(duration.total_seconds()) // 60
    hours_total, minutes = divmod(minutes_total, 60)
    days, hours = divmod(hours_total, 24)
    return {"days": days, "hours": hours, "minutes": minutes}


# Кабинетте әр тізімнің бір слайдында (карусель) көрсетілетін сессия саны
SESSIONS_PER_SLIDE = 2


def chunked(items, size):
    """Тізімді size-тан бөледі: [1, 2, 3] → [[1, 2], [3]] (карусель слайдтары)."""
    return [items[index : index + size] for index in range(0, len(items), size)]


def dashboard_sessions(user, now=None):
    """
    Кабинеттегі үш тізім: алдағы, ашық және өткен сессиялар, және олардың
    карусель слайдтары (әр слайдта SESSIONS_PER_SLIDE сессия).

    Шекаралар: opens_at <= уақыт < closes_at — ашық, яғни opens_at сәтінде
    сессия ашылады, closes_at сәтінде жабылады. Әр жолда студенттің осы
    сессиядағы әрекеті де беріледі (болмаса — None).
    """
    if now is None:
        now = timezone.now()
    attempts = {attempt.session_id: attempt for attempt in user.attempts.all()}

    upcoming, open_now, past = [], [], []
    for session in visible_sessions(user).order_by("opens_at"):
        row = {"session": session, "attempt": attempts.get(session.pk)}
        if now < session.opens_at:
            row["opens_in"] = split_duration(session.opens_at - now)
            upcoming.append(row)
        elif now < session.closes_at:
            open_now.append(row)
        else:
            past.append(row)

    # Ашық сессиялар: әлі тапсырылмағандары бірінші — «Бастау» бірінші слайдта тұрсын
    open_now.sort(
        key=lambda row: bool(row["attempt"])
        and row["attempt"].status == Attempt.Status.FINISHED
    )
    # Өткен сессиялар — ең соңғысы бірінші
    past.reverse()
    return {
        "upcoming": upcoming,
        "open": open_now,
        "past": past,
        "upcoming_slides": chunked(upcoming, SESSIONS_PER_SLIDE),
        "open_slides": chunked(open_now, SESSIONS_PER_SLIDE),
        "past_slides": chunked(past, SESSIONS_PER_SLIDE),
    }


def session_status(session, now=None):
    """Оқытушының сессиялар тізімі үшін: inactive / upcoming / open / closed."""
    if now is None:
        now = timezone.now()
    if not session.is_active:
        return "inactive"
    if now < session.opens_at:
        return "upcoming"
    if now < session.closes_at:
        return "open"
    return "closed"


def is_session_open(session, now=None):
    """Сессия қазір ашық па: белсенді және opens_at <= уақыт < closes_at."""
    if now is None:
        now = timezone.now()
    return session.is_active and session.opens_at <= now < session.closes_at


# ---------- Тест нұсқасын құру (TZ.md, 4-бөлім) ----------


class AttemptError(Exception):
    """Тестті бастау мүмкін емес (сессия жабық, бұрын басталған немесе банк толық емес)."""


def bank_error(language):
    """Банкте сұрақ жетпегенде студентке көрсетілетін қате."""
    return AttemptError(
        _(
            "Банкте «%(language)s» тіліндегі сұрақтар жеткіліксіз, тестті бастау "
            "мүмкін емес. Оқытушыға хабарлаңыз."
        )
        % {"language": Language(language).label}
    )


def choose_contexts(subject, language, rng):
    """
    1-қадам: 2 толық контекстті кездейсоқ таңдайды.

    Деңгейлері квотадан (13/30/7) аспайтын жұп бірінші алынады, сонда жеке
    сұрақтармен квотаны дәл толтыруға болады. Қайтарады: контексттер тізімі
    және әр контекстің сұрақтары {контекст id: [(сұрақ id, деңгей), ...]}.
    """
    contexts = list(full_contexts(subject, language))
    if len(contexts) < CONTEXTS_PER_TEST:
        logger.warning("Банкте «%s» тілінде толық контекст жеткіліксіз.", language)
        raise bank_error(language)

    context_questions = {context.pk: [] for context in contexts}
    rows = (
        Question.objects.filter(context__in=contexts, is_active=True)
        .order_by("id")
        .values_list("id", "context_id", "level")
    )
    for question_id, context_id, level in rows:
        context_questions[context_id].append((question_id, level))

    rng.shuffle(contexts)
    pairs = list(combinations(contexts, CONTEXTS_PER_TEST))
    for pair in pairs:
        levels = Counter(
            level for context in pair for _id, level in context_questions[context.pk]
        )
        if all(levels[level] <= LEVEL_QUOTA[level] for level in Level.values):
            return list(pair), context_questions

    logger.warning("«%s» тілінде деңгейі квотаға сыятын контекст жұбы жоқ.", language)
    return list(pairs[0]), context_questions


def find_swap(chosen, pool, remaining):
    """
    Деңгей квотасын түзететін алмастыру іздейді: таңдалған сұрақтың орнына
    сол тақырыпшадағы жетпейтін деңгейдің бос сұрағын қоюға бола ма.

    Алдымен артық деңгейдегі сұрақ алмастырылады. Ондай жол болмаса — квотасы
    дәл толған деңгейдегі сұрақ: сонда жетіспеушілік сол деңгейге ауысады да,
    келесі қадамда басқа тақырыпшада түзетіледі.
    Қайтарады: (тақырыпша id, сұрақтың орны, керек деңгей, ескі деңгей) немесе None.
    """
    needed = [level for level in Level.values if remaining[level] > 0]
    over = [level for level in Level.values if remaining[level] < 0]
    full = [level for level in Level.values if remaining[level] == 0]
    for replaceable in (over, full):
        for need in needed:
            for subtopic_id, items in chosen.items():
                for index, (_id, level) in enumerate(items):
                    if level in replaceable and pool[subtopic_id][need]:
                        return subtopic_id, index, need, level
    return None


def choose_single_questions(subject, language, quota, rng):
    """
    3–4-қадамдар: 20 тақырыпшаның әрқайсысынан 2 жеке сұрақ.

    quota — жеке сұрақтарға қалған деңгей квотасы, мысалы {"A": 11, "B": 24, "C": 5}.
    Әр сұрақ үшін алдымен квотасы бітпеген деңгей таңдалады (қалған квотаға
    қарай кездейсоқ). Ондай сұрақ тақырыпшада жоқ болса — басқа деңгей алынады.
    Соңында квотаны дәлдеу үшін сұрақтар тақырыпша ішінде алмастырылады;
    бәрібір дәл шықпаса (банкте қажет деңгей жетпейді) — журналға ескерту.
    Қайтарады: {тақырыпша id: [(сұрақ id, деңгей), ...]} тақырыпша ретімен.
    """
    # Бос сұрақтар: тақырыпша → деңгей → сұрақ id тізімі (кездейсоқ ретпен)
    subtopic_ids = (
        Subtopic.objects.filter(topic__subject=subject)
        .order_by("number")
        .values_list("id", flat=True)
    )
    pool = {subtopic_id: {level: [] for level in Level.values} for subtopic_id in subtopic_ids}
    rows = Question.objects.filter(
        subtopic__topic__subject=subject,
        language=language,
        is_active=True,
        context__isnull=True,
    ).values_list("id", "subtopic_id", "level")
    for question_id, subtopic_id, level in rows:
        pool[subtopic_id][level].append(question_id)
    for levels in pool.values():
        for question_ids in levels.values():
            rng.shuffle(question_ids)

    remaining = dict(quota)
    chosen = {subtopic_id: [] for subtopic_id in pool}

    # Тақырыпшаларды кездейсоқ ретпен өтеміз, сонда A/C деңгейлері әр нұсқада
    # әртүрлі тақырыпшаларға түседі
    subtopic_ids = list(pool)
    rng.shuffle(subtopic_ids)
    for subtopic_id in subtopic_ids:
        levels = pool[subtopic_id]
        for _slot in range(SINGLE_QUESTIONS_PER_SUBTOPIC):
            available = [level for level in Level.values if levels[level]]
            if not available:
                logger.warning(
                    "«%s» тілінде тақырыпшада (id=%s) жеке сұрақ жеткіліксіз.",
                    language,
                    subtopic_id,
                )
                raise bank_error(language)
            preferred = [level for level in available if remaining[level] > 0]
            if preferred:
                weights = [remaining[level] for level in preferred]
                level = rng.choices(preferred, weights=weights)[0]
            else:
                level = rng.choice(available)
            chosen[subtopic_id].append((levels[level].pop(), level))
            remaining[level] -= 1

    # Квотаны дәлдеу: сұрақтарды тақырыпша ішінде алмастыру.
    # Қадам саны шектеулі — банк жеткіліксіз болса, шексіз айналмайды.
    for _step in range(QUESTIONS_TOTAL):
        swap = find_swap(chosen, pool, remaining)
        if swap is None:
            break
        subtopic_id, index, need, old_level = swap
        old_id, _level = chosen[subtopic_id][index]
        pool[subtopic_id][old_level].append(old_id)
        chosen[subtopic_id][index] = (pool[subtopic_id][need].pop(), need)
        remaining[need] -= 1
        remaining[old_level] += 1

    if any(remaining.values()):
        logger.warning(
            "«%s» тіліндегі нұсқа деңгей квотасына дәл сәйкес емес "
            "(жетпейтіні оң, артығы теріс: %s): банкте қажет деңгейдегі сұрақ жоқ.",
            language,
            remaining,
        )
    return chosen


def build_variant(subject, language, rng=None):
    """
    Нұсқа құру алгоритмі (TZ.md, 4-бөлім), тек берілген пән мен тілдегі
    белсенді сұрақтардан.

    Қайтарады: 50 сұрақтың id тізімі тест ретімен — 1–40 жеке сұрақтар
    тақырыпша ретімен (01–20), 41–50 екі контекстің сұрақтары.
    rng — кездейсоқ сандар генераторы (тесттерде қайталанатын нәтиже үшін).
    """
    if rng is None:
        rng = random.Random()

    # 1-қадам: 2 контекст және олардың 10 сұрағы
    contexts, context_questions = choose_contexts(subject, language, rng)

    # 2-қадам: контекст сұрақтарының деңгейін санап, қалған квотаны есептеу
    context_levels = Counter(
        level for context in contexts for _id, level in context_questions[context.pk]
    )
    quota = {level: LEVEL_QUOTA[level] - context_levels[level] for level in Level.values}

    # 3–4-қадамдар: әр тақырыпшадан 2 жеке сұрақ
    chosen = choose_single_questions(subject, language, quota, rng)

    # 5-қадам: жеке сұрақтар тақырыпша ретімен, соңында контексттер
    question_ids = [question_id for items in chosen.values() for question_id, _level in items]
    for context in contexts:
        question_ids += [question_id for question_id, _level in context_questions[context.pk]]

    if len(question_ids) != QUESTIONS_TOTAL:
        logger.warning("«%s» тіліндегі нұсқада %s сұрақ шықты.", language, len(question_ids))
        raise bank_error(language)
    return question_ids


def variant_summary(subject, question_ids):
    """
    Нұсқаның қорытындысы (оқытушының «Үлгі нұсқа» беті үшін): сұрақтар тест
    ретімен және ережелердің орындалуы — сұрақ саны, A/B/C квотасы, әр
    тақырыпшадан 2 жеке сұрақ, 2 контекст × 5 сұрақ. Дерекқорға ештеңе жазбайды.
    """
    by_id = Question.objects.select_related("subtopic__topic", "context").in_bulk(
        question_ids
    )
    questions = [by_id[question_id] for question_id in question_ids]

    level_counts = Counter(question.level for question in questions)
    levels = [
        {
            "level": level,
            "count": level_counts[level],
            "quota": LEVEL_QUOTA[level],
            "ok": level_counts[level] == LEVEL_QUOTA[level],
        }
        for level in Level.values
    ]

    single_counts = Counter(
        question.subtopic_id for question in questions if question.context_id is None
    )
    subtopics = [
        {
            "subtopic": subtopic,
            "count": single_counts[subtopic.pk],
            "ok": single_counts[subtopic.pk] == SINGLE_QUESTIONS_PER_SUBTOPIC,
        }
        for subtopic in Subtopic.objects.filter(topic__subject=subject).select_related("topic")
    ]

    # Контексттер нұсқадағы ретімен (dict кірістіру ретін сақтайды)
    context_counts = {}
    for question in questions:
        if question.context_id:
            context_counts.setdefault(question.context, 0)
            context_counts[question.context] += 1
    contexts = [
        {"context": context, "count": count, "ok": count == QUESTIONS_PER_CONTEXT}
        for context, count in context_counts.items()
    ]

    checks = {
        "total": len(questions) == QUESTIONS_TOTAL,
        "levels": all(item["ok"] for item in levels),
        "subtopics": all(item["ok"] for item in subtopics),
        "contexts": len(contexts) == CONTEXTS_PER_TEST
        and all(item["ok"] for item in contexts),
    }
    return {
        "questions": questions,
        "total": len(questions),
        "levels": levels,
        "subtopics": subtopics,
        "contexts": contexts,
        "checks": checks,
        "all_ok": all(checks.values()),
        "questions_total": QUESTIONS_TOTAL,
        "contexts_per_test": CONTEXTS_PER_TEST,
        "questions_per_context": QUESTIONS_PER_CONTEXT,
        "per_subtopic": SINGLE_QUESTIONS_PER_SUBTOPIC,
    }


def attempt_deadline(session, started_at):
    """Тест мерзімі: min(басталған уақыт + пәннің тест уақыты, сессияның жабылуы)."""
    return min(
        started_at + timedelta(minutes=session.subject.duration_minutes), session.closes_at
    )


@transaction.atomic
def create_attempt(user, session, language, now=None, rng=None):
    """
    Тестті бастау: сессия ашық екенін және студент бұл сессияда бұрын
    бастамағанын тексереді, нұсқа құрып, әр сұрақтың жауап нұсқаларын араластырады.
    deadline = min(басталған уақыт + пәннің тест уақыты, сессияның жабылуы).
    """
    if now is None:
        now = timezone.now()
    if rng is None:
        rng = random.Random()

    if not is_session_open(session, now):
        raise AttemptError(_("Бұл сессия қазір ашық емес."))
    if Attempt.objects.filter(user=user, session=session).exists():
        raise AttemptError(_("Сіз бұл сессияда тестті бұрын бастағансыз."))

    question_ids = build_variant(session.subject, language, rng)

    try:
        # Екі сұраныс қатар келсе (батырма екі рет басылса), екіншісі
        # (user, session) шектеуіне соғылады — жаңа әрекет жасалмайды
        with transaction.atomic():
            attempt = Attempt.objects.create(
                user=user,
                session=session,
                language=language,
                started_at=now,
                deadline=attempt_deadline(session, now),
            )
    except IntegrityError:
        raise AttemptError(_("Сіз бұл сессияда тестті бұрын бастағансыз."))

    # Әр сұрақтың жауап нұсқалары: {сұрақ id: [жауап id, ...]}
    answers = {question_id: [] for question_id in question_ids}
    rows = Answer.objects.filter(question_id__in=question_ids).values_list(
        "question_id", "id"
    )
    for question_id, answer_id in rows:
        answers[question_id].append(answer_id)

    items = []
    for order, question_id in enumerate(question_ids, start=1):
        answer_order = sorted(answers[question_id])
        rng.shuffle(answer_order)
        items.append(
            AttemptQuestion(
                attempt=attempt,
                question_id=question_id,
                order=order,
                answer_order=answer_order,
            )
        )
    AttemptQuestion.objects.bulk_create(items)
    return attempt


# ---------- Тест тапсыру (TZ.md, 3-бөлім, 6-тармақ) ----------

# Жауап нұсқаларының студентке көрінетін әріптері
ANSWER_LETTERS = "ABCD"


def is_expired(attempt, now=None):
    """Әрекеттің мерзімі өтті ме: deadline сәтінен бастап жауап қабылданбайды."""
    if now is None:
        now = timezone.now()
    return now >= attempt.deadline


def remaining_seconds(attempt, now=None):
    """Мерзімге дейін қалған секундтар (таймер үшін; мерзім өтсе — 0)."""
    if now is None:
        now = timezone.now()
    # Жоғары қарай дөңгелектейміз: таймер сервердегі мерзімнен ерте нөлге жетпеуі үшін
    return max(0, math.ceil((attempt.deadline - now).total_seconds()))


@transaction.atomic
def finish_attempt(attempt, now=None):
    """
    Тестті аяқтайды: балл есептеп (дұрыс = 1, қате немесе бос = 0), күйін
    «Аяқталды» етеді. Бұрын аяқталған әрекет өзгермейді.
    Аяқталу уақыты мерзімнен кеш жазылмайды (уақыт біткен соң ашылса да).
    Қайтарады: жаңартылған әрекет.
    """
    if now is None:
        now = timezone.now()
    # Жолды құлыптаймыз: бір мезгілде жауап сақталса немесе тест екі рет
    # аяқталса, балл қате есептелмейді (SQLite бұл құлыпты елемейді)
    locked = Attempt.objects.select_for_update().get(pk=attempt.pk)
    if locked.status == Attempt.Status.FINISHED:
        return locked

    locked.score = locked.items.filter(selected__is_correct=True).count()
    locked.finished_at = min(now, locked.deadline)
    locked.status = Attempt.Status.FINISHED
    locked.save(update_fields=["score", "finished_at", "status"])
    return locked


def finish_if_expired(attempt, now=None):
    """
    Мерзімі өткен, бірақ аяқталмаған әрекетті аяқтайды (әрекет кез келген
    бетте ашылғанда шақырылады). Қайтарады: өзекті әрекет.
    """
    if attempt.status == Attempt.Status.IN_PROGRESS and is_expired(attempt, now):
        return finish_attempt(attempt, now)
    return attempt


def finish_expired_attempts(attempts=None, now=None):
    """
    Мерзімі өткен барлық аяқталмаған әрекеттерді аяқтайды (кабинет, оқытушы
    нәтижелері және `finish_expired` командасы үшін).
    attempts — тек осы әрекеттер ішінен (мысалы, бір студенттікі).
    Қайтарады: аяқталған әрекеттер саны.
    """
    if now is None:
        now = timezone.now()
    if attempts is None:
        attempts = Attempt.objects.all()
    expired = attempts.filter(status=Attempt.Status.IN_PROGRESS, deadline__lte=now)
    count = 0
    for attempt in expired:
        finish_attempt(attempt, now)
        count += 1
    return count


@transaction.atomic
def save_answer(item, answer_id, now=None):
    """
    Студенттің жауабын сақтайды (аяқталғанға дейін өзгертуге болады).
    answer_id — осы сұрақтың нұсқаларының бірі (forms.py тексереді).
    Тест аяқталса немесе мерзімі өтсе, жауап қабылданбайды — онда False қайтарады.
    """
    if now is None:
        now = timezone.now()
    attempt = Attempt.objects.select_for_update().get(pk=item.attempt_id)
    if attempt.status != Attempt.Status.IN_PROGRESS or is_expired(attempt, now):
        return False
    item.selected_id = answer_id
    item.save(update_fields=["selected"])
    return True


def first_unanswered_number(attempt):
    """Жауап берілмеген бірінші сұрақтың нөмірі (бәріне жауап берілсе — 1)."""
    item = attempt.items.filter(selected__isnull=True).order_by("order").first()
    return item.order if item else 1


def unanswered_numbers(attempt):
    """Жауап берілмеген сұрақтардың нөмірлері (аяқтауды растау беті үшін)."""
    return list(
        attempt.items.filter(selected__isnull=True)
        .order_by("order")
        .values_list("order", flat=True)
    )


def ordered_answers(answer_order, answers_by_id):
    """
    Сұрақтың жауап нұсқалары студентке көрсетілген ретпен:
    [{"letter": "A", "answer": Answer}, ...]. answer_order — нұсқалар id тізімі.
    """
    return [
        {"letter": ANSWER_LETTERS[index], "answer": answers_by_id[answer_id]}
        for index, answer_id in enumerate(answer_order)
    ]


def attempt_page_data(attempt, now=None):
    """
    Тест бетіне керек деректер — барлық 50 сұрақ бір бетте (біреуі ғана
    көрінеді, static/js/attempt.js бетті қайта жүктемей ауыстырады):
    әр сұрақтың нұсқалары араласқан ретімен, таңдалған жауабы, алдыңғы/келесі
    нөмірлері; 1–50 навигация, жауап берілмегендер саны, қалған уақыт.
    Нұсқалардың тек id-і, мәтіні мен суреті беріледі: is_correct шаблонға жетпейді.
    """
    items = list(attempt.items.select_related("question__context").order_by("order"))
    answer_ids = [answer_id for item in items for answer_id in item.answer_order]
    answers_by_id = Answer.objects.in_bulk(answer_ids)
    total = len(items)

    questions = []
    for item in items:
        answers = [
            {
                "letter": row["letter"],
                "id": row["answer"].pk,
                "text": row["answer"].text,
                "image": row["answer"].image,
            }
            for row in ordered_answers(item.answer_order, answers_by_id)
        ]
        questions.append(
            {
                "number": item.order,
                "question": item.question,
                "answers": answers,
                "selected_id": item.selected_id,
                "previous_number": item.order - 1 if item.order > 1 else None,
                "next_number": item.order + 1 if item.order < total else None,
            }
        )

    return {
        "questions": questions,
        "navigation": [
            {"number": item.order, "answered": item.selected_id is not None} for item in items
        ],
        "unanswered_count": sum(1 for item in items if item.selected_id is None),
        "total": total,
        "remaining_seconds": remaining_seconds(attempt, now),
    }


def active_attempt(user, now=None):
    """
    Студенттің қазір жүріп жатқан тесті (мерзімі өтпеген, аяқталмаған) немесе None.
    Тест кезінде аккаунттан шығуға болмайды (accounts.views.logout_view).
    """
    if now is None:
        now = timezone.now()
    if not user.is_authenticated:
        return None
    return (
        Attempt.objects.filter(user=user, status=Attempt.Status.IN_PROGRESS, deadline__gt=now)
        .order_by("deadline")
        .first()
    )


# ---------- Оқытушының нәтижелер беті және CSV (TZ.md, 3-бөлім, 8-тармақ) ----------


def results_rows(attempts):
    """
    Нәтижелер кестесінің жолдары: әрекет, студенттің аты-жөні, тобы, пайызы
    және жұмсалған уақыты (минут). attempts — select_related жасалған тізім.
    """
    rows = []
    for attempt in attempts:
        profile = getattr(attempt.user, "profile", None)
        spent_minutes = None
        if attempt.finished_at:
            spent_minutes = int((attempt.finished_at - attempt.started_at).total_seconds()) // 60
        rows.append(
            {
                "attempt": attempt,
                "student": attempt.user.get_full_name() or attempt.user.username,
                "group": profile.group if profile and profile.group else None,
                "percent": percent(attempt.score or 0, QUESTIONS_TOTAL),
                "spent_minutes": spent_minutes,
            }
        )
    return rows


def results_summary(attempts):
    """Сүзілген нәтижелер бойынша қысқаша: әрекет саны, аяқталғаны, орташа балл."""
    finished = [
        attempt.score or 0
        for attempt in attempts
        if attempt.status == Attempt.Status.FINISHED
    ]
    average = round(sum(finished) / len(finished), 1) if finished else None
    return {"count": len(attempts), "finished": len(finished), "average": average}


# Excel бұл таңбалардан басталатын ұяшықты формула деп орындайды (CSV инъекциясы)
CSV_FORMULA_CHARS = ("=", "+", "-", "@", chr(9), chr(13))  # chr(9) — tab, chr(13) — CR


def csv_safe(value):
    """
    Студент өзі енгізген мәтінді (аты-жөні, логин) CSV-ге қауіпсіз жазу:
    формула болып орындалмауы үшін алдына апостроф қойылады.
    """
    text = str(value)
    if text.startswith(CSV_FORMULA_CHARS):
        return "'" + text
    return text


def write_results_csv(attempts, output):
    """
    Нәтижелерді CSV етіп output-қа жазады (HttpResponse немесе файл).
    Excel кириллицаны дұрыс ашуы үшін UTF-8 BOM және «;» бөлгіші қолданылады.
    """
    output.write("﻿")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            _("Студент"),
            _("Логин"),
            _("Топ"),
            _("Пән"),
            _("Сессия"),
            _("Тест тілі"),
            _("Басталды"),
            _("Аяқталды"),
            _("Күйі"),
            _("Балл"),
            _("Пайыз"),
            _("Уақыт (мин)"),
        ]
    )

    def local(moment):
        return timezone.localtime(moment).strftime("%d.%m.%Y %H:%M") if moment else ""

    for row in results_rows(attempts):
        attempt = row["attempt"]
        is_finished = attempt.status == Attempt.Status.FINISHED
        writer.writerow(
            [
                csv_safe(row["student"]),
                csv_safe(attempt.user.username),
                csv_safe(row["group"] or ""),
                attempt.session.subject.name,
                csv_safe(attempt.session.title),
                attempt.get_language_display(),
                local(attempt.started_at),
                local(attempt.finished_at),
                attempt.get_status_display(),
                attempt.score if is_finished else "",
                row["percent"] if is_finished else "",
                row["spent_minutes"] if is_finished else "",
            ]
        )


# ---------- Нәтиже (TZ.md, 3-бөлім, 7-тармақ) ----------


def can_see_answers(attempt, user, now=None):
    """
    Әр сұрақтың дұрыс жауабын көрсетуге бола ма. Оқытушыға — әрқашан.
    Студентке — тек тест аяқталған соң, сессия баптауына қарай:
    аяқтаған бойда немесе сессия жабылғаннан кейін.
    """
    if user.is_staff:
        return True
    if attempt.status != Attempt.Status.FINISHED:
        return False
    if attempt.session.show_answers == ExamSession.ShowAnswers.AFTER_CLOSE:
        if now is None:
            now = timezone.now()
        return now >= attempt.session.closes_at
    return True


def percent(correct, total):
    """Дұрыс жауаптар пайызы (бүтін сан)."""
    return round(correct * 100 / total) if total else 0


def attempt_result(attempt, with_answers):
    """
    Әрекеттің нәтижесі: балл, пайыз, жұмсалған уақыт, A/B/C деңгейлері және
    сессия пәнінің тақырыптары бойынша дұрыс жауаптар кестесі.
    with_answers=True болса — әр сұрақтың студент жауабы мен дұрыс жауабы да.
    """
    items = list(
        attempt.items.select_related(
            "question__subtopic__topic", "question__context", "selected"
        ).order_by("order")
    )
    total = len(items)
    correct_items = [item for item in items if item.selected and item.selected.is_correct]
    score = len(correct_items)

    # Деңгей бойынша: A, B, C
    level_total = Counter(item.question.level for item in items)
    level_correct = Counter(item.question.level for item in correct_items)
    levels = [
        {
            "label": label,
            "correct": level_correct[level],
            "total": level_total[level],
            "percent": percent(level_correct[level], level_total[level]),
        }
        for level, label in Level.choices
    ]

    # Тақырып бойынша: пәннің барлық тақырыбы (информатика — 11, көркем еңбек — 5, ...)
    topic_total = Counter(item.question.subtopic.topic_id for item in items)
    topic_correct = Counter(item.question.subtopic.topic_id for item in correct_items)
    topics = [
        {
            "topic": topic,
            "correct": topic_correct[topic.pk],
            "total": topic_total[topic.pk],
            "percent": percent(topic_correct[topic.pk], topic_total[topic.pk]),
        }
        for topic in Topic.objects.filter(subject_id=attempt.session.subject_id)
    ]

    spent_seconds = 0
    if attempt.finished_at:
        spent_seconds = int((attempt.finished_at - attempt.started_at).total_seconds())
    spent_minutes, spent_rest = divmod(spent_seconds, 60)

    questions = []
    if with_answers:
        answer_ids = [answer_id for item in items for answer_id in item.answer_order]
        answers_by_id = Answer.objects.in_bulk(answer_ids)
        for item in items:
            questions.append(
                {
                    "number": item.order,
                    "question": item.question,
                    "answers": ordered_answers(item.answer_order, answers_by_id),
                    "selected_id": item.selected_id,
                    "is_correct": bool(item.selected and item.selected.is_correct),
                }
            )

    return {
        "score": score,
        "total": total,
        "percent": percent(score, total),
        "spent_minutes": spent_minutes,
        "spent_seconds": spent_rest,
        "levels": levels,
        "topics": topics,
        "questions": questions,
    }


# ---------- Тақырыптық жаттығу (TZ.md, 3-бөлім, 10-тармақ; екінші кезең) ----------
# Жаттығу дерекқорға жазылмайды: оның күйі студенттің Django сессиясында
# сақталатын қарапайым сөздік — {"topic": id, "language": "kk",
# "items": [{"question": id, "answer_order": [...], "selected": id немесе None}]}


def start_practice(topic, language, rng=None):
    """
    Жаттығуды бастау: тақырыптың берілген тілдегі белсенді сұрақтарынан
    кездейсоқ 10-ын (аз болса — барын) таңдап, жауап нұсқаларын араластырады.
    Сұрақ мүлде болмаса — AttemptError.
    """
    if rng is None:
        rng = random.Random()
    question_ids = list(
        Question.objects.filter(
            subtopic__topic=topic, language=language, is_active=True
        ).values_list("id", flat=True)
    )
    if not question_ids:
        raise AttemptError(_("Бұл тақырып бойынша таңдалған тілде сұрақ әлі жоқ."))
    question_ids = rng.sample(question_ids, min(PRACTICE_QUESTIONS, len(question_ids)))

    answers = {question_id: [] for question_id in question_ids}
    rows = Answer.objects.filter(question_id__in=question_ids).values_list("question_id", "id")
    for question_id, answer_id in rows:
        answers[question_id].append(answer_id)

    items = []
    for question_id in question_ids:
        answer_order = sorted(answers[question_id])
        rng.shuffle(answer_order)
        items.append({"question": question_id, "answer_order": answer_order, "selected": None})
    return {"topic": topic.pk, "language": language, "items": items}


def practice_answer(practice, number, answer_id):
    """
    Жаттығудағы n-сұраққа жауап жазады. Жауап бір рет беріледі (дұрысы бірден
    көрсетілетіндіктен, кейін өзгертуге болмайды). Қайтарады: жазылды ма.
    """
    item = practice["items"][number - 1]
    if item["selected"] is not None:
        return False
    item["selected"] = answer_id
    return True


def practice_page_data(practice):
    """
    Жаттығу беті — барлық сұрақ бір бетте (біреуі ғана көрінеді, static/js/practice.js
    бетті қайта жүктемей ауыстырады). Әр сұрақтың нұсқалары араласқан ретімен;
    дұрыс жауап тек студент жауап берген сұрақта шаблонға жетеді.
    Сұрақтың бірі банктен өшіріліп кетсе — None (жаттығуды қайта бастау керек).
    """
    items = practice["items"]
    question_ids = [item["question"] for item in items]
    questions_by_id = Question.objects.select_related(
        "context", "subtopic__topic__subject"
    ).in_bulk(question_ids)
    if len(questions_by_id) != len(set(question_ids)):
        return None
    answer_ids = [answer_id for item in items for answer_id in item["answer_order"]]
    answers_by_id = Answer.objects.in_bulk(answer_ids)
    total = len(items)

    questions = []
    for number, item in enumerate(items, start=1):
        answered = item["selected"] is not None
        answers = []
        for row in ordered_answers(item["answer_order"], answers_by_id):
            answer = row["answer"]
            answers.append(
                {
                    "letter": row["letter"],
                    "id": answer.pk,
                    "text": answer.text,
                    "image": answer.image,
                    # Дұрыс жауап тек студент жауап бергеннен кейін шаблонға жетеді
                    "is_correct": answer.is_correct if answered else None,
                    "is_selected": answer.pk == item["selected"],
                }
            )
        questions.append(
            {
                "number": number,
                "question": questions_by_id[item["question"]],
                "answers": answers,
                "answered": answered,
                "is_correct": answered
                and any(a["is_selected"] and a["is_correct"] for a in answers),
                "previous_number": number - 1 if number > 1 else None,
                "next_number": number + 1 if number < total else None,
            }
        )

    first_question = questions[0]["question"] if questions else None
    return {
        "questions": questions,
        "total": total,
        "navigation": practice_navigation(practice),
        "uses_formulas": bool(
            first_question and first_question.subtopic.topic.subject.uses_formulas
        ),
    }


def practice_answer_feedback(practice, number):
    """
    n-сұраққа жауап берілгеннен кейінгі нәтиже (practice.js үшін JSON):
    таңдалған жауап, дұрыс жауап және дұрыс па. Сұрақтың жауаптары
    банктен өшіріліп кетсе — None.
    """
    item = practice["items"][number - 1]
    correct_id = (
        Answer.objects.filter(pk__in=item["answer_order"], is_correct=True)
        .values_list("id", flat=True)
        .first()
    )
    if correct_id is None:
        return None
    return {
        "selected_id": item["selected"],
        "correct_id": correct_id,
        "is_correct": item["selected"] == correct_id,
    }


def practice_correct_ids(practice):
    """Жаттығуда дұрыс жауап берілген сұрақтардың нөмірлері (1-ден бастап)."""
    selected_ids = [item["selected"] for item in practice["items"] if item["selected"]]
    correct = set(
        Answer.objects.filter(pk__in=selected_ids, is_correct=True).values_list("id", flat=True)
    )
    return {
        number
        for number, item in enumerate(practice["items"], start=1)
        if item["selected"] in correct
    }


def practice_navigation(practice):
    """Навигация: әр сұрақ — жауап берілмеген / дұрыс / қате."""
    correct_numbers = practice_correct_ids(practice)
    navigation = []
    for number, item in enumerate(practice["items"], start=1):
        if item["selected"] is None:
            state = "empty"
        elif number in correct_numbers:
            state = "correct"
        else:
            state = "wrong"
        navigation.append({"number": number, "state": state})
    return navigation


def practice_summary(practice):
    """Жаттығу қорытындысы: тақырып, дұрыс жауаптар саны, жауап берілмегені."""
    total = len(practice["items"])
    correct = len(practice_correct_ids(practice))
    unanswered = sum(1 for item in practice["items"] if item["selected"] is None)
    return {
        "topic": Topic.objects.filter(pk=practice["topic"]).first(),
        "language": Language(practice["language"]).label,
        "correct": correct,
        "total": total,
        "unanswered": unanswered,
        "percent": percent(correct, total),
        "navigation": practice_navigation(practice),
    }
