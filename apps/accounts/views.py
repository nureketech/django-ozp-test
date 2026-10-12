from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.quiz.services import active_attempt, first_unanswered_number

# Студенттерді қосу, өңдеу, импорттау беттері — student_views.py (/students/...)


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
