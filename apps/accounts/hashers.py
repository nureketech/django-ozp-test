from django.contrib.auth.hashers import PBKDF2PasswordHasher


class AccessCodeHasher(PBKDF2PasswordHasher):
    """
    Студенттің кіру коды импорт кезінде осы хэшермен сақталады: итерация саны
    азырақ, сондықтан бірнеше жүз студентті импорттау бір минутқа созылмайды
    (әдепкі хэшер — бір кодқа ~0,3 с). Студент бірінші кіргенде Django кодты
    әдепкі (күштірек) хэшермен қайта сақтайды.
    """

    algorithm = "pbkdf2_access_code"
    iterations = 100_000
