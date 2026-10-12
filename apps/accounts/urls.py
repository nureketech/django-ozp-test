from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from .forms import LoginForm

app_name = "accounts"

urlpatterns = [
    # Тіркелу жоқ: студенттерді әкімші admin-де Excel-ден импорттайды (ИИН + код)
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="accounts/login.html",
            authentication_form=LoginForm,
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    # Шығу тек POST арқылы (навигациядағы «Шығу» батырмасы — форма);
    # тест жүріп жатқанда шығуға болмайды (views.logout_view)
    path("logout/", views.logout_view, name="logout"),
    path("after-login/", views.after_login, name="after_login"),
]
