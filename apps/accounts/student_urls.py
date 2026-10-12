from django.urls import path

from . import student_views

app_name = "students"

# «Студенттер» беттері — тек әкімші (student_views.superuser_required)
urlpatterns = [
    path("", student_views.student_list, name="list"),
    path("add/", student_views.student_add, name="add"),
    path("<int:pk>/edit/", student_views.student_edit, name="edit"),
    path("<int:pk>/new-code/", student_views.student_new_code, name="new_code"),
    path("<int:pk>/delete/", student_views.student_delete, name="delete"),
    path("import/", student_views.student_import, name="import"),
    path("import/template/", student_views.student_import_template, name="import_template"),
]
