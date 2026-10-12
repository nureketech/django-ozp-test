# VPS-ке орнату нұсқаулығы (Ubuntu 24.04)

Бұл нұсқаулық сайтты таза Ubuntu 24.04 серверге қадамдап орнатады: Nginx + Gunicorn (systemd) + PostgreSQL + HTTPS (Let's Encrypt).

Төменде қолданылатын атаулар (қаласаңыз өзгертіңіз, бірақ барлық файлда бірдей болсын):

| Не | Атауы |
| --- | --- |
| Жүйе пайдаланушысы | `ozp` |
| Жоба папкасы | `/srv/ozp-test` |
| Деректер қоры / оның пайдаланушысы | `ozp_test` / `ozp` |
| systemd сервисі | `ozp-test` |
| Домен | `test.example.kz` — өз доменіңізге ауыстырыңыз |

`$` — қарапайым пайдаланушы атынан, `sudo` — әкімші құқығымен орындалатын командалар.

## 1. Пайдаланушы, SSH кілті, брандмауэр

Серверге root ретінде кіріп, өзіңізге жеке пайдаланушы жасаңыз (мысалы, `admin`):

```bash
adduser admin
usermod -aG sudo admin
# Өз компьютеріңіздегі SSH кілтін көшіру (компьютерде орындалады):
#   ssh-copy-id admin@СЕРВЕР_IP
```

`admin` ретінде кіре алатыныңызды тексергеннен кейін root кіруін және құпия сөзбен кіруді өшіріңіз (`/etc/ssh/sshd_config`: `PermitRootLogin no`, `PasswordAuthentication no`, сосын `sudo systemctl restart ssh`).

Брандмауэр — тек 22, 80, 443 порттары:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'   # 80 және 443 (Nginx орнатылғаннан кейін; әйтпесе: sudo ufw allow 80,443/tcp)
sudo ufw enable
sudo ufw status
```

## 2. Python, PostgreSQL, Nginx

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-dev git postgresql nginx gettext certbot python3-certbot-nginx
```

Деректер қоры мен оның пайдаланушысы (`КҮШТІ_ҚҰПИЯ_СӨЗ` орнына өз құпия сөзіңізді жазыңыз):

```bash
sudo -u postgres psql -c "CREATE USER ozp WITH PASSWORD 'КҮШТІ_ҚҰПИЯ_СӨЗ';"
sudo -u postgres psql -c "CREATE DATABASE ozp_test OWNER ozp ENCODING 'UTF8';"
```

## 3. Код, виртуалды орта, `.env`

Сайт жұмыс істейтін жеке жүйе пайдаланушысы (оған SSH арқылы кірілмейді):

```bash
sudo adduser --system --group --no-create-home --home /srv/ozp-test ozp
sudo mkdir /srv/ozp-test
sudo chown ozp:ozp /srv/ozp-test
sudo -u ozp git clone https://github.com/ҰЙЫМ/django-ozp-test.git /srv/ozp-test
cd /srv/ozp-test
sudo -u ozp python3 -m venv .venv
sudo -u ozp .venv/bin/pip install --no-cache-dir -r requirements.txt
```

> Репозиторий жабық (private) болса, `git clone` үшін GitHub-та deploy key немесе токен керек.

`.env` файлы:

```bash
sudo -u ozp cp .env.example .env
sudo -u ozp nano .env
sudo chmod 600 .env
```

Мәндері:

```ini
SECRET_KEY=<жаңа кездейсоқ кілт: .venv/bin/python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())">
DEBUG=False
ALLOWED_HOSTS=test.example.kz
DATABASE_URL=postgres://ozp:КҮШТІ_ҚҰПИЯ_СӨЗ@localhost:5432/ozp_test
CSRF_TRUSTED_ORIGINS=https://test.example.kz
SECURE_HSTS_SECONDS=0
```

## 4. Деректер қоры, статика, аударма, әкімші

```bash
cd /srv/ozp-test
sudo -u ozp .venv/bin/python manage.py migrate
sudo -u ozp .venv/bin/python manage.py collectstatic --noinput
sudo -u ozp .venv/bin/python manage.py compilemessages --ignore=.venv
sudo -u ozp .venv/bin/python manage.py load_subjects
sudo -u ozp .venv/bin/python manage.py createsuperuser
sudo -u ozp .venv/bin/python manage.py check --deploy
```

`check --deploy` тек `SECURE_HSTS_SECONDS` туралы ескерту беруі мүмкін — ол 6-қадамнан кейін қосылады.

Жүктелген суреттер папкасы: `sudo -u ozp mkdir -p media`.

> **Пәндер:** `load_subjects` барлық пәнді, тақырыптарды және тақырыпшаларды `apps/quiz/data/subjects.json`-нан жүктейді (қайта іске қосуға болады). Жаңа пән қосылса немесе `subjects.json` түзетілсе, жаңартудан кейін осы команданы қайта орындаңыз. Оқытушыларды әкімші admin-де қосады, студенттерді Excel-ден импорттайды (README, «Студенттерді қосу», «Оқытушыны қосу»). Сайтта тіркелу жоқ.
>
> **Демо сұрақтар:** серверде `load_demo` іске қоспаңыз. Егер тексеру үшін жүктелсе, нақты пайдалануға дейін міндетті түрде өшіріңіз: `sudo -u ozp .venv/bin/python manage.py load_demo --delete`.

## 5. Gunicorn (systemd) және Nginx

```bash
sudo cp deploy/gunicorn.service /etc/systemd/system/ozp-test.service
sudo systemctl daemon-reload
sudo systemctl enable --now ozp-test
sudo systemctl status ozp-test        # active (running) болуы керек

sudo cp deploy/nginx.conf /etc/nginx/sites-available/ozp-test
sudo nano /etc/nginx/sites-available/ozp-test   # server_name — өз доменіңіз
sudo ln -s /etc/nginx/sites-available/ozp-test /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Nginx `/srv/ozp-test/staticfiles` және `media` папкаларын оқи алуы керек: `sudo chmod 755 /srv/ozp-test`.

> **Кіру бетіне шектеу.** `deploy/nginx.conf` кіру бетіндегі POST сұраныстарын шектейді (`limit_req`: бір IP-ден минутына 30, бірден 60-қа дейін — бір сыныптың студенттері бір IP-ден қатар кіре алады; асса — 429). Студенттер бір мезгілде өте көп болып, 429 шықса, `burst` мәнін көбейтіңіз. Бұрынғы баптау орнатылған серверде жаңарғанда `nginx.conf`-ты қайта көшіріп, `sudo nginx -t && sudo systemctl reload nginx` орындаңыз.

## 6. Домен және HTTPS

1. Домен тіркеушісінің бетінде `A` жазбасын сервердің IP мекенжайына бағыттаңыз; `ping test.example.kz` IP-ді көрсеткенше күтіңіз.
2. Сертификат алу (certbot nginx баптауына 443 портты және HTTP → HTTPS бағыттауды өзі қосады):

```bash
sudo certbot --nginx -d test.example.kz
sudo certbot renew --dry-run     # сертификат өзі жаңаратынын тексеру
```

3. Сайт `https://test.example.kz` арқылы ашылатынын тексеріңіз. Барлығы дұрыс болса, `.env` ішінде `SECURE_HSTS_SECONDS=31536000` қойып, `sudo systemctl restart ozp-test`.

## 7. Бэкап, cron және жаңарту

Күнделікті бэкап (деректер қоры + суреттер, соңғы 14 күн, `/var/backups/ozp-test`):

```bash
sudo cp deploy/backup.sh /usr/local/bin/ozp-backup.sh
sudo chmod +x /usr/local/bin/ozp-backup.sh
sudo /usr/local/bin/ozp-backup.sh          # бір рет қолмен тексеру
sudo crontab -e
```

`crontab` ішіне:

```cron
# Күн сайын 03:30-да бэкап
30 3 * * * /usr/local/bin/ozp-backup.sh >> /var/log/ozp-backup.log 2>&1
```

Мерзімі өткен тесттерді 5 минут сайын аяқтау (`sudo crontab -u ozp -e`):

```cron
*/5 * * * * cd /srv/ozp-test && .venv/bin/python manage.py finish_expired > /dev/null
```

**Жаңарту тәртібі** (жаңа код шыққанда):

```bash
cd /srv/ozp-test
sudo -u ozp git pull
sudo -u ozp .venv/bin/pip install --no-cache-dir -r requirements.txt
sudo -u ozp .venv/bin/python manage.py migrate
sudo -u ozp .venv/bin/python manage.py collectstatic --noinput
sudo -u ozp .venv/bin/python manage.py compilemessages --ignore=.venv
# subjects.json өзгерсе:
sudo -u ozp .venv/bin/python manage.py load_subjects
sudo systemctl restart ozp-test
```

## Бэкаптан қалпына келтіру

```bash
sudo systemctl stop ozp-test
sudo -u postgres psql -c "DROP DATABASE ozp_test;"
sudo -u postgres psql -c "CREATE DATABASE ozp_test OWNER ozp ENCODING 'UTF8';"
# Көшірме ozp атынан жүктеледі — кестелер соған тиесілі болады
gunzip -c /var/backups/ozp-test/db-2026-10-15.sql.gz \
    | psql "postgres://ozp:КҮШТІ_ҚҰПИЯ_СӨЗ@localhost:5432/ozp_test"
sudo tar -xzf /var/backups/ozp-test/media-2026-10-15.tar.gz -C /srv/ozp-test
sudo chown -R ozp:ozp /srv/ozp-test/media
sudo systemctl start ozp-test
```

## Соңғы тексеру

- [ ] `https://test.example.kz` ашылады, `http://` мекенжайы HTTPS-ке бағытталады.
- [ ] `sudo -u ozp .venv/bin/python manage.py check --deploy` ескертусіз өтеді.
- [ ] Кіру, тіл ауыстырғыш (KK / RU) жұмыс істейді; статика (Bootstrap, таймер) жүктеледі; `/accounts/register/` — 404.
- [ ] Әкімші студенттер файлын импорттап, кодтар файлын алады; студент ИИН мен кодпен кіреді.
- [ ] Оқытушы сұрақтарды Excel-ден импорттай алады (`/teacher/questions/import/`).
- [ ] Оқытушы сурет жүктей алады және ол сұрақта көрінеді (`/media/`); тек суреттен тұратын жауап нұсқасы сақталады.
- [ ] Математика сұрағындағы `\(\frac{1}{2}\)` формула болып көрінеді (KaTeX, `cdn.jsdelivr.net`).
- [ ] `sudo reboot` → бірнеше минуттан кейін сайт өзі ашылады.
- [ ] Келесі күні `/var/backups/ozp-test` ішінде жаңа `db-*.sql.gz` файлы бар.
- [ ] `sudo ufw status` — тек 22, 80, 443.
- [ ] `load_demo --delete` орындалған: оқытушының «Сұрақтар» бетінде «демо» белгісі жоқ.

Қате болса: `sudo journalctl -u ozp-test -n 100` (Django/Gunicorn) және `sudo tail -n 50 /var/log/nginx/error.log` (Nginx).
