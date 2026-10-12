from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.db.models import Q
from django.forms.models import BaseInlineFormSet, construct_instance
from django.utils.translation import gettext_lazy as _

from apps.quiz.forms import add_bootstrap_classes

from .models import Profile, StudyGroup
from .student_import import iin_is_valid, normalize_iin, split_name, student_name


class LoginForm(AuthenticationForm):
    """
    Кіру: студент — ИИН мен код (әкімші импорт арқылы береді), оқытушы — логин
    мен құпия сөз. Django-ның дайын формасы: қазақша мәтіндер және Bootstrap стилі.
    """

    error_messages = {
        "invalid_login": _("ИИН (логин) немесе код (құпия сөз) қате."),
        "inactive": _("Бұл тіркелгі бұғатталған."),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = _("ИИН немесе логин")
        self.fields["password"].label = _("Код немесе құпия сөз")
        add_bootstrap_classes(self)

    def clean_username(self):
        # ИИН-ді «050312 650123» деп бос орынмен терсе де кіре алады
        return "".join(self.cleaned_data["username"].split())


class GroupChoiceField(forms.ModelChoiceField):
    """Топтар тізімі: «ИНФ-21 — Информатика (Қазақша)» — топтың пәні мен тест тілі көрінсін."""

    def label_from_instance(self, group):
        return f"{group.name} — {group.subject.name} ({group.get_language_display()})"


class StudentForm(forms.Form):
    """
    «Студенттер» беттерінде студент қосу және өңдеу (тек әкімші): аты-жөні, тобы,
    ИИН. Тексерулер Excel импортымен бірдей. user — өңделетін студент (қосқанда None).
    ИИН-мен басқа студент бұрыннан бар болса — existing_user (шаблон оның бетіне
    сілтеме көрсетеді).
    """

    full_name = forms.CharField(
        label=_("Аты-жөні"),
        max_length=300,
        help_text=_("Тегі, аты, әкесінің аты: бірінші сөз — тегі, қалғаны — аты."),
    )
    group = GroupChoiceField(
        label=_("Топ"),
        queryset=StudyGroup.objects.select_related("subject").order_by("name"),
    )
    iin = forms.CharField(label=_("ИИН"), max_length=20)

    def __init__(self, *args, user=None, **kwargs):
        if user is not None and "initial" not in kwargs:
            profile = getattr(user, "profile", None)
            kwargs["initial"] = {
                "full_name": student_name(user),
                "group": profile.group_id if profile else None,
                "iin": user.username,
            }
        super().__init__(*args, **kwargs)
        self.user = user
        self.existing_user = None
        self.fields["iin"].widget.attrs["inputmode"] = "numeric"
        add_bootstrap_classes(self)

    def clean_full_name(self):
        full_name = " ".join(self.cleaned_data["full_name"].split())
        if any(len(part) > 150 for part in split_name(full_name)):
            raise forms.ValidationError(_("тым ұзын."))
        return full_name

    def clean_iin(self):
        iin = normalize_iin(self.cleaned_data["iin"])
        if not iin_is_valid(iin):
            raise forms.ValidationError(_("12 цифрдан тұрып, бақылау цифры дұрыс болуы керек."))
        other = User.objects.filter(username=iin)
        if self.user is not None:
            other = other.exclude(pk=self.user.pk)
        other = other.first()
        if other is not None and (other.is_staff or other.is_superuser):
            raise forms.ValidationError(_("бұл — қызметкердің логині, оны өзгертуге болмайды."))
        if other is not None:
            self.existing_user = other
            raise forms.ValidationError(_("Бұл ИИН-мен студент бұрыннан бар."))
        return iin


class StudentFilterForm(forms.Form):
    """Студенттер тізімінің сүзгісі (GET): топ және аты-жөні не ИИН бойынша іздеу."""

    group = GroupChoiceField(
        label=_("Топ"),
        queryset=StudyGroup.objects.select_related("subject").order_by("name"),
        required=False,
        empty_label=_("Барлық топ"),
    )
    q = forms.CharField(label=_("Іздеу"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = _("Аты-жөні немесе ИИН")
        add_bootstrap_classes(self)

    def filter(self, users):
        """Сүзгіні студенттер тізіміне қолданады (сүзгі қате болса — тізім өзгермейді)."""
        if not self.is_valid():
            return users
        if self.cleaned_data["group"]:
            users = users.filter(profile__group=self.cleaned_data["group"])
        for word in self.cleaned_data["q"].split():
            users = users.filter(
                Q(username__icontains=word)
                | Q(last_name__icontains=word)
                | Q(first_name__icontains=word)
            )
        return users


class ProfileInlineFormSet(BaseInlineFormSet):
    """
    Admin-дегі профиль (топ, оқытушының пәндері). Жаңа қолданушы сақталғанда
    профильді post_save сигналы жасап қояды, сондықтан inline жаңа профиль
    жасамай, сол профильді толтырады (әйтпесе UNIQUE қатесі шығады).
    """

    def save_new(self, form, commit=True):
        profile = Profile.objects.filter(user=self.instance).first()
        if profile is None:
            return super().save_new(form, commit=commit)
        form.instance = construct_instance(form, profile)
        return form.save(commit=commit)
