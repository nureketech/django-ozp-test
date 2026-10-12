from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from .forms import ProfileInlineFormSet
from .models import Profile, StudyGroup
from .student_import import codes_workbook, regenerate_codes
from .student_views import xlsx_response


def codes_file_response(request, rows):
    """Жаңа кодтар файлы (немесе студент таңдалмаса — ескерту)."""
    if not rows:
        messages.warning(request, _("Таңдалғандардың арасында студент жоқ."))
        return None
    messages.success(request, _("%(count)s студентке жаңа код жасалды.") % {"count": len(rows)})
    return xlsx_response(codes_workbook(rows), "student-codes")


def only_for_superuser(actions, request, names):
    """Кодтармен жұмыс істейтін әрекеттер тек әкімшіге көрінеді."""
    if not request.user.is_superuser:
        for name in names:
            actions.pop(name, None)
    return actions


@admin.register(StudyGroup)
class StudyGroupAdmin(admin.ModelAdmin):
    """Топтар — тек әкімші (Django-ның әдепкі рұқсаттары; оқытушыға ашық емес)."""

    list_display = ["name", "subject", "language", "student_count"]
    list_filter = ["subject", "language"]
    search_fields = ["name"]
    # Тізімнің үстінде «Студенттерді басқару» батырмасы (тек әкімшіге) — сайттағы /students/
    change_list_template = "admin/accounts/studygroup/change_list.html"
    actions = ["regenerate_group_codes"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(students=Count("profiles"))

    @admin.display(description=_("студенттер"), ordering="students")
    def student_count(self, obj):
        return obj.students

    def get_actions(self, request):
        return only_for_superuser(super().get_actions(request), request, ["regenerate_group_codes"])

    @admin.action(description=_("Топ студенттеріне жаңа код жасау"))
    def regenerate_group_codes(self, request, queryset):
        users = User.objects.filter(profile__group__in=queryset)
        return codes_file_response(request, regenerate_codes(users))


class ProfileInline(admin.StackedInline):
    model = Profile
    formset = ProfileInlineFormSet
    can_delete = False
    # Пәндер тек оқытушыға тағайындалады
    filter_horizontal = ["subjects"]


# Қолданушы бетінде профильді (топ, пәндер) бірге көрсету
admin.site.unregister(User)


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    """
    Оқытушы тек осы жерде қосылады (сайтта тек студент тіркеледі): әкімші
    бір бетте логин, құпия сөз, аты-жөнін енгізіп, «Қызметкер мәртебесі»
    (is_staff) белгілейді және төмендегі профильде пәндерін таңдайды.
    """

    inlines = [ProfileInline]
    add_fieldsets = (
        (None, {"fields": ("username", "usable_password", "password1", "password2")}),
        (_("Аты-жөні"), {"fields": ("first_name", "last_name")}),
        (
            _("Оқытушы"),
            {
                "fields": ("is_staff",),
                "description": _(
                    "Оқытушы болса белгілеңіз және төмендегі профильде пәндерін таңдаңыз."
                ),
            },
        ),
    )
    list_display = ["username", "first_name", "last_name", "group_name", "is_staff"]
    list_filter = ["is_staff", "is_superuser", "profile__subjects", "profile__group"]
    actions = ["regenerate_user_codes"]

    def get_actions(self, request):
        return only_for_superuser(super().get_actions(request), request, ["regenerate_user_codes"])

    @admin.action(description=_("Студенттерге жаңа код жасау"))
    def regenerate_user_codes(self, request, queryset):
        return codes_file_response(request, regenerate_codes(queryset))

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        user = form.instance
        # Пәні жоқ оқытушы /teacher/ беттерінде тек «пән тағайындалмаған» хабарламасын көреді
        if user.is_staff and not user.is_superuser and not user.profile.subjects.exists():
            messages.warning(
                request,
                _("%(user)s — оқытушы, бірақ оған пән тағайындалмаған.") % {"user": user},
            )

    @admin.display(description=_("топ"))
    def group_name(self, obj):
        # Профилі жоқ қолданушы үшін де қате бермейді
        profile = getattr(obj, "profile", None)
        if profile and profile.group:
            return profile.group
        return "—"
