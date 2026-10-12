from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Интерфейс тілін ауыстыру (Django-ның дайын set_language, POST)
    path("i18n/", include("django.conf.urls.i18n")),
    path("accounts/", include("apps.accounts.urls")),
    # Студенттерді қосу, өңдеу, импорттау — тек әкімші
    path("students/", include("apps.accounts.student_urls")),
    path("", include("apps.quiz.urls")),
]

# Әзірлеу кезінде жүктелген суреттерді Django өзі береді (серверде — Nginx)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
