# CLAUDE.md

Бірнеше пән (информатика, көркем еңбек ұл/қыз, математика) бойынша ПББ тестін жаттықтыратын Django платформасы. Толық талаптар — `TZ.md` (көп пән — 10-бөлім).

## Стек

- Python 3.12+, **Django 5.2 LTS**
- Деректер қоры: **PostgreSQL** (VPS), **SQLite** (жергілікті әзірлеу)
- Интерфейс: Django шаблондары + **Bootstrap 5 (CDN)**
- Суреттер: Pillow, `media/`
- `USE_TZ = True`, `TIME_ZONE = "Asia/Almaty"`
- **Қолданылмайды:** DRF (REST API), React, Celery, Docker. Django-дан басқа күрделі кітапхана қоспа (рұқсат етілген жалғыз қосымша кітапхана — Excel үшін **`openpyxl`**).

## Жоба құрылымы

- Барлық Django қосымшалары `apps/` папкасында: `apps.accounts`, `apps.quiz`.
- `INSTALLED_APPS` ішінде `"apps.accounts"`, `"apps.quiz"`; әр `apps.py`-да `name = "apps.<атауы>"`.
- **Жаңа қосымша да `apps/` ішіне жасалады** (`apps/<атауы>/`, `name = "apps.<атауы>"`).
- Баптаулар — `config/` (settings.py, urls.py, wsgi.py); шаблондар — `templates/`; JS — `static/js/`.
- Тест құрылымы (50 сұрақ, 20 тақырыпша × 2, 2 × 5 контекст, 13/30/7) барлық пәнге ортақ — тек `apps/quiz/constants.py` ішінде.
- Пәннен пәнге өзгеретіні (тақырыптар, тест уақыты 80 / 125 мин, `uses_formulas`) — `Subject` моделінде; деректері `apps/quiz/data/subjects.json`.

## Көп пән

- `Subject` (apps.quiz): `code`, `name_kk`, `name_ru`, `duration_minutes`, `uses_formulas`, `order`, `is_active`.
- `Topic`, `Context`, `ExamSession`, `StudyGroup` — `subject` FK (міндетті, PROTECT). `Question`-да пән жоқ: `question.subtopic.topic.subject`; контекст пен сұрақтың пәні бірдей болуы керек.
- `Topic.number` пән ішінде бірегей (`UniqueConstraint(subject, number)`); `Subtopic.number` (01–20) де пән ішінде ғана бірегей — **тақырыпшаны нөмірімен іздегенде әрқашан пән бойынша сүз**.
- Оқытушының пәндері — `Profile.subjects` (M2M), `is_superuser` — барлық пән (`services.teacher_subjects`); студенттің пәні — тобының пәні (`profile.group.subject`); сессияның топтары — сол пәннің топтары.
- Оқытушының таңдалған пәні `request.session["teacher_subject_id"]`-да (навбардағы «Пән» ауыстырғышы, тек бірнеше пәні болса). Пәні жоқ оқытушыға — «Сізге пән тағайындалмаған» беті (403).
- Банк пен нұсқа сервистері пәнді параметр ретінде алады: `build_variant(subject, language)`, `full_contexts(subject, language)`, `bank_coverage(subject)`, `variant_summary(subject, ids)`; `create_attempt()` пәнді сессиядан алады.
- Тест мерзімі: `deadline = min(started_at + session.subject.duration_minutes, session.closes_at)`.
- **Тест тілі — топтың оқыту тілі** (`StudyGroup.language`, міндетті, әдепкісі жоқ — топ құрған әкімші/оқытушы көрсетеді). Студент тест тілін таңдамайды (`services.student_language`); жаттығуда топ тілі тек әдепкі мән.
- Студент жағы — тек тобының пәні (`services.student_subject`): сессиялар (`visible_sessions`: пәні бірдей және топтары бос не тобы ішінде; тобы жоқ — ештеңе), жаттығу тақырыптары, нәтижедегі тақырыптар талдауы.
- **Формулалар (KaTeX, CDN):** тек `subject.uses_formulas` пәнінде және тек сұрақ беттерінде — view `uses_formulas` береді, `base.html` `quiz/_katex.html`-ды қосады; `static/js/formulas.js` `\( \)` / `\[ \]` өңдейді (`pre.code-block`, `code`, форма өрістері — жоқ). Мәтін өзгеріссіз сақталады, `|safe` жоқ.
- **Жауап нұсқасы:** мәтін және/немесе сурет (`Answer.image`, файл аты uuid); екеуі де бос — қате (`forms.answer_has_content`). Сурет — jpg/png/webp, 2 МБ (`constants.MAX_IMAGE_MB`, `models.IMAGE_VALIDATORS`). Жауапты шаблонда `quiz/_answer_content.html` көрсетеді.
- **Жаңа пән кодты өзгертпей қосылады:** `subjects.json`-ға жазылып, `load_subjects` іске қосылады. Пәнге тән мән кодта жазылмайды (тек бұрынғы деректер үшін `constants.INFORMATICS_CODE`).

## Код жазу ережелері

- **Views тек функциялар (FBV).** Класқа негізделген views жазба (тек Django-ның дайын `LoginView` / `set_language` қолданылады).
- **Тест беті мен жаттығу беті AJAX-пен:** барлық сұрақ бір бетте (`services.attempt_page_data` / `practice_page_data`), біреуі көрінеді. Ортақ `static/js/question_pages.js` сұрақты бетті жүктемей ауыстырады (URL — `history.pushState`), жауаптарды кезекпен `fetch` арқылы жібереді (`X-Requested-With: XMLHttpRequest` → view JSON қайтарады, `views.is_ajax`); бетке тәні — `attempt.js` / `practice.js`. Жаттығуда дұрыс жауап тек жауап берілген соң JSON-да келеді (`practice_answer_feedback`). JS жоқ болса — бұрынғыдай форма + redirect. Жаңа JS тек progressive enhancement болсын: JS-сіз де жұмыс істеуі керек.
- **Бизнес-логика** (нұсқа құру, балл есептеу, банк толуы) — `apps/quiz/services.py`; Excel импорттары — `apps/quiz/question_import.py` және `apps/accounts/student_import.py`.
- **Форма тексерулері** — `apps/quiz/forms.py` (accounts үшін `apps/accounts/forms.py`). Views ішінде логика да, тексеру де болмайды.
- Оқытушы беттері — `apps/quiz/teacher_views.py`, `@staff_member_required` + `@subject_required` (view `request, subject, ...` алады); **барлық сұраныс таңдалған пән бойынша сүзіледі**, басқа пәннің объектісі — 404 (`get_object_or_404(subject_questions(subject), pk=pk)`). Формаларға `subject=` беріледі. Студент беттері — `@login_required`.
- **Айнымалы/функция атаулары ағылшынша, түсініктемелер қазақша.** PEP 8.
- Код қарапайым болсын: жаңадан бастаған әзірлеуші оқи алатындай, артық абстракциясыз.

## Excel импорты және кіру

- Excel файлдары — `openpyxl`; жүктеу формасы ортақ: `quiz.forms.ExcelImportForm` (.xlsx, `constants.IMPORT_MAX_MB`). Бір парақта ең көбі `constants.IMPORT_MAX_ROWS` жол; бағандар тақырыбы бойынша емес, **реті бойынша** оқылады (1-жол — тақырыптар). Тексеру «бәрі немесе ештеңе»: қате болса — ештеңе сақталмайды, қателер жол нөмірімен.
- **Сұрақтар импорты** (`question_import.py`, бет `/teacher/questions/import/`, `@subject_required`): «Сұрақтар» және «Контексттер» парақтары (атаулары аударылмайды — импорт оларды атымен іздейді). Тақырыпша пән бойынша ізделеді; бар сұрақ (мәтіні, тілі, тақырыпшасы бірдей) өткізіледі, бар контекст (атауы, тілі, мәтіні) қайта жасалмайды. **Суреттер Excel ішінен импортталады** (ұяшықтың үстіне қойылған сурет, сол жақ жоғарғы бұрышы тұрған ұяшыққа тиесілі): сұрақ/контекст суреті — «Сурет» бағанында (соңғы баған), жауап суреті — сол жауаптың A–D ұяшығында; суреті бар жауаптың мәтіні бос болуы мүмкін. Файл толық ашылады (`read_only` суреттерді оқымайды).
- **Студенттер импорты** (`student_import.py`, admin: `StudyGroupAdmin.get_urls` → `accounts.views.import_students_view`, тек `is_superuser`): Аты-жөні, Топ, ИИН. Логин — ИИН (бақылау цифры тексеріледі, `iin_is_valid`), құпия сөз — 8 цифрлы код; кодтың хэші ғана сақталады, өзі тек жүктелетін кодтар файлында. Бар ИИН — аты-жөні мен тобы жаңартылады, коды өзгермейді; «Жаңа код жасау» — admin әрекеттері. Бір студентті қолмен қосу — `accounts.views.add_student_view` (`StudentAddForm`, `student_import.create_student`): код экранда бір рет көрсетіледі (`never_cache`).
- Кодтар `accounts.hashers.AccessCodeHasher` (100 000 итерация) арқылы хэштеледі — импорт жылдам; бірінші кіргенде Django оны негізгі хэшерге ауыстырады (`PASSWORD_HASHERS`-та бірінші тұрған).
- Кіру — Django-ның `LoginView` + `accounts.forms.LoginForm` («ИИН немесе логин» / «Код немесе құпия сөз», бос орындар алынады). Nginx кіру бетіне POST шектеуін қояды (`deploy/nginx.conf`, `limit_req`).

## Екі тіл (i18n)

- Интерфейс **қазақша (негізгі, `kk`) және орысша (`ru`)** — Django i18n, `LocaleMiddleware`.
- Интерфейстегі **барлық мәтін** шаблонда `{% translate %}` / `{% blocktranslate %}`, Python-да `gettext` / `gettext_lazy` ішінде жазылады. Бастапқы мәтін — қазақша.
- Аударма файлдары: `locale/kk/`, `locale/ru/` (`makemessages` / `compilemessages`).
- Сұрақтардың мазмұны аударылмайды — әр сұрақтың өз тілі бар (`language` өрісі).

## Қауіпсіздік

- `is_correct` тест аяқталмайынша HTML-ге шықпайды.
- Студент тек өз `Attempt`-ын және өз тобының сессияларын көреді (бөтеніне 404).
- Оқытушы тек өз пәндерінің сұрақтарын, контексттерін, нәтижелерін көреді (басқа пәннікі — 404).
- **Сайтта тіркелу жоқ.** Оқытушыны әкімші admin-де қосады (`is_staff` + `Profile.subjects`); студенттерді тек әкімші (`is_superuser`) Excel-ден импорттайды — оқытушыға 403. Admin-де оқытушыға тек сессиялар мен топтар ашық (`TeacherSubjectAdminMixin`: өз пәндері ғана, өшіру — әкімшіге).
- Сессия уақыты мен тест мерзімін тек сервер тексереді.
- **Тест жүріп жатқанда аккаунттан шығуға болмайды:** `accounts.views.logout_view` (FBV, тек POST) `services.active_attempt` бар болса тест бетіне қайтарады; навбарда «Шығу» бұғатталып, «Тестке оралу» шығады (`quiz.context_processors.attempt_in_progress`).
- `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `DATABASE_URL` — `.env` файлынан.

## Жергілікті орта

Python виртуалды ортасы — **`.venv`** (Windows):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py load_subjects
.venv\Scripts\python manage.py load_demo
.venv\Scripts\python manage.py runserver
```

## Тесттер

- Іске қосу: `python manage.py test` (`.venv\Scripts\python manage.py test`).
- **Әр кезеңнен кейін тесттерді міндетті түрде іске қос** және нәтижесін хабарла; жаңа функционалға тест жаз.

## Деректер

- `load_subjects` — `subjects.json`-нан барлық пәнді, тақырыптарды, тақырыпшаларды жүктейді (4 пән: 11 / 5 / 5 / 20 тақырып, әрқайсында 20 тақырыпша); `--only <code>` — бір пән. Алдымен тексереді (дәл 20 тақырыпша, 1–20, атаулар бос емес), қате болса ештеңе жазбайды; қайта іске қосуға болады.
- `load_topics` — `load_subjects --only informatics` (бұрынғы команда).
- «Информатика» пәні деректер миграциясында жасалады (`quiz/0003`): бұрынғы деректер соған байланған.
- `load_demo` — тақырыптары жүктелген әр пәнге демо сұрақтар мен контексттер (әр пәнде толық нұсқа құрылады), барлығы `is_demo=True`; `--subject <code>` — бір пән; `load_demo --delete [--subject <code>]` оларды толық өшіреді (суреттерімен).
- `loadtest_data [--subject <code>]` — жүктеме тестінің аккаунттары мен сессиясы (әдепкі пән — informatics).

## Git

- Әр кезең аяқталып, тесттер өткен соң commit жаса. **Push жасама.**
