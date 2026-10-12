from django.contrib.auth.forms import AuthenticationForm
from django.forms.models import BaseInlineFormSet, construct_instance
from django.utils.translation import gettext_lazy as _

from apps.quiz.forms import add_bootstrap_classes

from .models import Profile


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
