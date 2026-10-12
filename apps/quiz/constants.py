"""
Тест құрылымының барлық сандары осы жерде (TZ.md, 4-бөлім, 10.1).
Сандар барлық пәнге бірдей; пәннен пәнге өзгеретіні (тақырыптар, тест уақыты)
`Subject` моделінде (`apps/quiz/data/subjects.json`).
"""

# Бір нұсқадағы сұрақтар саны
QUESTIONS_TOTAL = 50

# Жеке сұрақтар: 20 тақырыпшаның әрқайсысынан 2 сұрақ = 40
SUBTOPICS_COUNT = 20
SINGLE_QUESTIONS_PER_SUBTOPIC = 2

# Контекстік сұрақтар: 2 контекст × 5 сұрақ = 10 (тесттің соңында, 41–50)
CONTEXTS_PER_TEST = 2
QUESTIONS_PER_CONTEXT = 5

# Қиындық деңгейлері бойынша квота: A 26%, B 60%, C 14%
LEVEL_QUOTA = {"A": 13, "B": 30, "C": 7}

# Әр сұрақта 4 жауап нұсқасы, біреуі дұрыс
ANSWERS_PER_QUESTION = 4

# Банктің ең аз көлемі (әр тіл үшін бөлек)
MIN_QUESTIONS_PER_SUBTOPIC = 6
MIN_CONTEXTS = 4

# Тақырыптық жаттығу (екінші кезең): бір тақырыптан сұрақ саны, таймерсіз
PRACTICE_QUESTIONS = 10

# Сұрақ пен жауап нұсқасының суреті: ең үлкен өлшемі (МБ) және рұқсат етілген пішімдер
MAX_IMAGE_MB = 2
IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]

# Сұрақтарды Excel-ден импорттау: файлдың ең үлкен өлшемі (МБ) және жол саны
IMPORT_MAX_MB = 5
IMPORT_MAX_ROWS = 2000

# Бұрынғы (бір пәнді) деректердің пәні: load_topics, load_demo, loadtest_data
INFORMATICS_CODE = "informatics"
