from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.forms.models import BaseInlineFormSet, construct_instance
from django.utils.translation import gettext_lazy as _

from apps.quiz.forms import add_bootstrap_classes

from .models import Profile, StudyGroup
from .student_import import iin_is_valid, normalize_iin, split_name


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


class StudentAddForm(forms.Form):
    """
    Admin-дегі «Студент қосу» (тек әкімші): аты-жөні, тобы, ИИН. Тексерулер
    Excel импортымен бірдей. ИИН-мен студент бұрыннан бар болса — existing_user
    (шаблон оның бетіне сілтеме көрсетеді).
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.existing_user = None
        self.fields["iin"].widget.attrs["inputmode"] = "numeric"

    def clean_full_name(self):
        full_name = " ".join(self.cleaned_data["full_name"].split())
        if any(len(part) > 150 for part in split_name(full_name)):
            raise forms.ValidationError(_("тым ұзын."))
        return full_name

    def clean_iin(self):
        iin = normalize_iin(self.cleaned_data["iin"])
        if not iin_is_valid(iin):
            raise forms.ValidationError(_("12 цифрдан тұрып, бақылау цифры дұрыс болуы керек."))
        user = User.objects.filter(username=iin).first()
        if user is not None and user.is_staff:
            raise forms.ValidationError(_("бұл — қызметкердің логині, оны өзгертуге болмайды."))
        if user is not None:
            self.existing_user = user
            raise forms.ValidationError(_("Бұл ИИН-мен студент бұрыннан бар."))
        return iin


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
