"""Local HH assistant. Python standard library only; browser adapter uses Playwright."""
from __future__ import annotations
import argparse
import base64
import ctypes
from contextlib import contextmanager
import html
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hr_review import draft_reply, message_text
from reply_model import ReplyModel, ModelError

ROOT = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
DATA = Path(os.environ.get('HH_DATA_DIR', str(ROOT / 'data')))
MSK = timezone(timedelta(hours=3))
DEFAULTS = dict(min_salary=60000, target_salary=70000, interval_minutes=30,
                daily_limit=15, resume_id='', user_agent='TimurJobAssistant/0.2', chat_interval_minutes=5,
                auto_apply=False, auto_reply=False, enabled=False,
                model_provider='deepseek',model_name='deepseek-flash',model_enabled=False,
                queries=['бизнес-аналитик', 'системный аналитик', 'аналитик данных', 'BI аналитик', 'аналитик бизнес-процессов'])
PROFILE = json.loads(os.environ['HH_PROFILE_JSON']) if os.environ.get('HH_PROFILE_JSON') else json.loads((ROOT / 'profile.json').read_text(encoding='utf-8'))


def now():
    return datetime.now(MSK).isoformat(timespec='seconds')


def clean(value):
    return html.unescape(re.sub('<[^>]+>', ' ', str(value or ''))).strip()


class Store:
    def __init__(self, folder=DATA):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = self.folder / 'assistant.sqlite3'
        if os.environ.get('HH_DATABASE_URL'):
            from pg_store import initialize
            initialize()
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vacancies (id TEXT PRIMARY KEY, body TEXT NOT NULL, score INTEGER,
              reason TEXT, letter TEXT, status TEXT NOT NULL DEFAULT 'new', updated TEXT);
            CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, kind TEXT, ref TEXT, title TEXT,
              body TEXT, draft TEXT, status TEXT DEFAULT 'open', created TEXT, UNIQUE(kind,ref));
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT, kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, body TEXT, baseline TEXT);
            CREATE TABLE IF NOT EXISTS messages (chat_id TEXT, id TEXT, body TEXT, status TEXT,
              PRIMARY KEY(chat_id,id));
            CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, chat_id TEXT, text TEXT, status TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hr_reviews (task_id INTEGER PRIMARY KEY, body TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (json.dumps(DEFAULTS),))
            # Crash recovery: uncertain mutations are never automatically retried.
            db.execute("UPDATE vacancies SET status='uncertain' WHERE status='sending'")
            db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")
            db.execute("UPDATE tasks SET status='uncertain' WHERE status='sending'")
            db.execute("""INSERT OR IGNORE INTO tasks(kind,ref,title,body,draft,created)
                SELECT 'delivery',id,'Проверь доставку сообщения',
                'Чат '||chat_id||'. Программа была остановлена или не получила подтверждение. Проверь доставку в hh.ru. Текст: '||text,'',?
                FROM outbox WHERE status='uncertain'""",(now(),))

    @contextmanager
    def connect(self):
        if os.environ.get('HH_DATABASE_URL'):
            from pg_store import connect
            with connect() as db:
                yield db
            return
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def resume_context(self):
        with self.connect() as db:
            row=db.execute("SELECT body FROM documents WHERE name='resume'").fetchone()
        return row[0] if row else ''

    def settings(self):
        with self.connect() as db:
            saved=json.loads(db.execute('SELECT body FROM settings WHERE id=1').fetchone()[0])
            return {key:saved.get(key,value) for key,value in DEFAULTS.items()}

    def save_settings(self, incoming):
        current = self.settings()
        for key in DEFAULTS:
            if key not in incoming:
                continue
            v = incoming[key]
            if key in ('enabled', 'auto_apply', 'auto_reply', 'model_enabled'):
                if not isinstance(v, bool):
                    raise ValueError('Ожидалось логическое значение')
            elif key in ('min_salary', 'target_salary', 'interval_minutes', 'daily_limit', 'chat_interval_minutes'):
                v = int(v)
                limits = {'min_salary':(1,1000000),'target_salary':(1,1000000),'interval_minutes':(5,1440),'daily_limit':(1,50),'chat_interval_minutes':(2,60)}
                low, high = limits[key]
                if not low <= v <= high:
                    raise ValueError(f'Недопустимое значение {key}: {low}–{high}')
            elif key == 'queries':
                if not isinstance(v, list) or not 1 <= len(v) <= 10 or any(not isinstance(x,str) or not 1 <= len(x) <= 100 for x in v):
                    raise ValueError('Укажи 1–10 поисковых запросов')
            else:
                v = str(v).strip()
                if len(v)>250 or '\r' in v or '\n' in v:
                    raise ValueError('Слишком длинное поле')
            if key == 'resume_id' and v:
                v = v.rstrip('/').split('/')[-1].split('?')[0]
                if not re.fullmatch('[a-zA-Z0-9]+', v):
                    raise ValueError('Укажи ID или ссылку на резюме hh.ru')
            current[key] = v
        if current['target_salary'] < current['min_salary']:
            raise ValueError('Ориентир по зарплате не может быть ниже минимума')
        if current['enabled'] and current['auto_apply'] and not current['resume_id']:
            raise ValueError('Сначала выбери и сохрани резюме или выключи автоотклики для запуска только поиска')
        with self.connect() as db:
            db.execute('UPDATE settings SET body=? WHERE id=1', (json.dumps(current,ensure_ascii=False),))
        return current

    def event(self, kind, detail):
        with self.connect() as db:
            db.execute('INSERT INTO events(at,kind,detail) VALUES (?,?,?)',(now(),kind,str(detail)[:1500]))

    def task(self, kind, ref, title, body, draft=''):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO tasks(kind,ref,title,body,draft,created) VALUES (?,?,?,?,?,?)',
                       (kind,str(ref),title,body,draft,now()))

    def snapshot(self):
        with self.connect() as db:
            vacancies = [dict(r) for r in db.execute('SELECT * FROM vacancies ORDER BY score DESC,updated DESC LIMIT 250')]
            for v in vacancies:
                v['body'] = json.loads(v['body'])
            tasks=[dict(r) for r in db.execute("SELECT tasks.*,COALESCE(hr_reviews.body,'{}') AS review FROM tasks LEFT JOIN hr_reviews ON hr_reviews.task_id=tasks.id WHERE status!='done' ORDER BY id DESC LIMIT 500")]
            for task in tasks:
                task['review']=json.loads(task['review'])
            return dict(settings=self.settings(), vacancies=vacancies,
                        tasks=tasks,
                        events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')],
                        outbox=[dict(r) for r in db.execute('SELECT * FROM outbox ORDER BY created DESC LIMIT 50')])


class Vault:
    """DPAPI binds secrets to the current Windows login. Never returned by HTTP."""
    def __init__(self, folder=DATA):
        self.path = Path(folder) / ('secrets.dpapi' if os.name=='nt' else 'secrets.encrypted')
        self.lock = threading.RLock()

    @staticmethod
    def crypt(data, decrypt=False):
        if os.name != 'nt' or os.environ.get('HH_DATABASE_URL'):
            import hashlib
            from cryptography.fernet import Fernet
            secret=os.environ.get('HH_MASTER_KEY','')
            if len(secret)<32:
                raise RuntimeError('Для серверного хранилища нужен HH_MASTER_KEY длиной от 32 символов')
            cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
            return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
        buf = ctypes.create_string_buffer(data)
        src = Blob(len(data), ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)))
        out = Blob()
        func = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
        func.argtypes = [ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        func.restype = wintypes.BOOL
        if not func(ctypes.byref(src),None,None,None,None,1,ctypes.byref(out)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out.pbData,out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.LocalFree(out.pbData)

    def read(self):
        with self.lock:
            if os.environ.get('HH_DATABASE_URL'):
                from pg_store import connect
                with connect() as db:
                    row=db.execute("SELECT body FROM documents WHERE name='encrypted_secrets'").fetchone()
                return json.loads(self.crypt(row[0].encode(),True)) if row else {}
            if not self.path.exists():
                return {}
            return json.loads(self.crypt(self.path.read_bytes(),True))

    def update(self, **values):
        with self.lock:
            current = self.read()
            current.update(values)
            blob = self.crypt(json.dumps(current).encode())
            if os.environ.get('HH_DATABASE_URL'):
                from pg_store import connect
                with connect() as db:
                    db.execute("INSERT OR REPLACE INTO documents VALUES ('encrypted_secrets',?)",(blob.decode(),))
                return
            temp = self.path.with_suffix('.tmp')
            temp.write_bytes(blob)
            os.replace(temp,self.path)


class BrowserActionError(ValueError):
    def __init__(self, detail, status='attention'):
        self.status=status
        super().__init__(detail)


class HHError(Exception):
    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(f'hh.ru HTTP {code}: {detail}')


class HH:
    def __init__(self, store, vault):
        self.store, self.vault = store,vault
        self.lock = threading.RLock()

    def request(self, path, params=None, method='GET', payload=None, auth=False, form=False):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Неверный путь API')
        agent=self.store.settings()['user_agent']
        if os.environ.get('HH_CONTACT_EMAIL'):
            agent+=' ('+os.environ['HH_CONTACT_EMAIL']+')'
        headers = {'User-Agent':agent,'HH-User-Agent':agent, 'Accept':'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer '+(self.application_token() if auth=='application' else self.token())
        url = 'https://api.hh.ru'+path
        if params:
            url += '?'+urllib.parse.urlencode(params,doseq=True)
        body = None
        if payload is not None:
            body = (urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode())
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        req = urllib.request.Request(url,data=body,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # Do not log response bodies containing candidate data or credentials.
            labels = {400:'проверь параметры запроса',401:'требуется повторная авторизация',403:'доступ запрещён для приложения или аккаунта',404:'объект недоступен',409:'ключ сообщения уже использован',429:'лимит запросов; цикл остановлен'}
            raise HHError(e.code,labels.get(e.code,'ошибка сервиса')) from None
        except (urllib.error.URLError, TimeoutError):
            raise HHError(0,'сеть недоступна или истекло время ожидания; результат отправки может быть неизвестен') from None

    def application_token(self):
        with self.lock:
            saved=self.vault.read()
            if saved.get('app_access_token') and (not saved.get('app_expires_at') or saved['app_expires_at']>time.time()+90):
                return saved['app_access_token']
            result=self.request('/token',method='POST',form=True,payload={
                'grant_type':'client_credentials','client_id':saved['api_client_id'],
                'client_secret':saved['client_secret']})
            if not result.get('access_token'):
                raise ValueError('hh.ru не вернул токен приложения')
            self.vault.update(app_access_token=result['access_token'],
                              app_expires_at=time.time()+int(result['expires_in']) if result.get('expires_in') else 0)
            return result['access_token']

    def token(self):
        with self.lock:
            sec = self.vault.read()
            if not sec.get('access_token'):
                raise ValueError('Подключи hh.ru API в настройках')
            if sec.get('expires_at',0)>time.time()+90:
                return sec['access_token']
            if not sec.get('refresh_token'):
                raise ValueError('Требуется повторная авторизация hh.ru')
            result = self.request('/token',method='POST',payload={
                'grant_type':'refresh_token','refresh_token':sec['refresh_token'],
                'client_id':sec['api_client_id'],'client_secret':sec.get('client_secret','')},form=True)
            self.save_tokens(result)
            return result['access_token']

    def save_tokens(self, result):
        if not result.get('access_token'):
            raise ValueError('hh.ru не вернул токен')
        update = dict(access_token=result['access_token'], expires_at=time.time()+int(result.get('expires_in',3600)))
        if result.get('refresh_token'):
            update['refresh_token']=result['refresh_token']
        self.vault.update(**update)


def excluded_employer(v):
    name=clean((v.get('employer') or {}).get('name')).casefold()
    return bool(re.search(r'(?<![a-zа-яё])(?:aston|астон)(?![a-zа-яё])',name))


def evaluate(v, settings):
    if excluded_employer(v):
        return 0,'Aston исключён по твоему пожеланию'
    name = clean(v.get('name')).lower()
    desc = clean(v.get('description') or json.dumps(v.get('snippet',{}),ensure_ascii=False)).lower()
    reasons=[]
    if v.get('archived'):
        return 0,'Вакансия в архиве'
    if re.search(r'senior|middle|lead|ведущ|старш|руководител|главн',name):
        return 0,'Уровень выше начального'
    if not re.search(r'аналитик|analyst|бизнес.анализ|системн. анализ',name):
        return 0,'Не аналитическая роль'
    if re.search(r'маркетинг|инвестиц|финансов|продаж|кредитн|товарн|закуп',name) and not re.search(r'бизнес|системн|данных|\bbi\b',name):
        return 20,'Смежная специализация: требуется оценка вручную'
    experience=(v.get('experience') or {}).get('id')
    if experience not in ('noExperience','between1And3'):
        return 0,'Допустимы «Без опыта» и «1–3 года»'
    area=str((v.get('area') or {}).get('id',''))
    remote=any(x.get('id')=='REMOTE' for x in v.get('work_format',[])) or (v.get('schedule') or {}).get('id')=='remote'
    if area!='1' and not remote:
        return 0,'Офис/гибрид за пределами Москвы'
    trip=(v.get('business_trip_readiness') or {}).get('id','') if isinstance(v.get('business_trip_readiness'),dict) else ''
    if trip in ('ready','sometimes') or re.search(r'(?:обязательн\w*|регулярн\w*|част\w*)\s+командиров',desc):
        return 0,'Требуются командировки'
    salary=v.get('salary') or v.get('salary_range')
    salary_uncertain=False
    if salary:
        if salary.get('_period_uncertain'):
            salary_uncertain=True
            reasons.append('Оплата не за месяц — уточнить размер дохода')
        if salary.get('currency')!='RUR':
            salary_uncertain=True
            reasons.append('Зарплата в другой валюте: уточнить')
        else:
            gross=salary.get('gross')
            # Conservative screening estimate, not payroll computation.
            factor=0.87 if gross is True else 1
            lower,upper=salary.get('from'),salary.get('to')
            if upper is not None and upper*factor<settings['min_salary']:
                return 0,'Верхняя граница зарплаты ниже минимума'
            if gross is None:
                salary_uncertain=True
                reasons.append('Неизвестно, до или после налогов')
            elif lower is None or lower*factor<settings['min_salary']:
                reasons.append('Вилка пересекает минимум: обсудить от 60 000 на руки')
            else:
                reasons.append('Зарплата подходит')
    else:
        reasons.append('Зарплата не указана — подходит для обсуждения')
    score=55
    if re.search(r'бизнес|системн|business|system',name):
        score+=15; reasons.append('Основное направление')
    score+=15; reasons.append('Без опыта работы' if experience=='noExperience' else 'Опыт 1–3 года')
    matches=sum(bool(re.search(p,desc)) for p in [r'\bsql\b',r'\bpython\b',r'power\s*bi',r'excel',r'бизнес.процесс',r'требован'])
    score+=min(matches*3,15)
    if not matches:
        score-=10; reasons.append('Совпадений по навыкам мало')
    if salary_uncertain:
        score=min(score,59)
    if v.get('response_url') or v.get('has_test') or v.get('response_letter_required') is None and v.get('type',{}).get('id')=='direct':
        score=min(score,59); reasons.append('Внешняя форма или тест — вручную')
    return min(score,100),'; '.join(reasons)


def letter_for(v):
    description=v.get('description') or json.dumps(v.get('snippet',{}),ensure_ascii=False)
    text=clean(re.sub(r'</(?:p|li|h[1-6])>', '\n',description,flags=re.I)).lower()
    duties=re.search(r'(?:обязанности|задачи|чем предстоит заниматься|тебе предстоит|вам предстоит)\s*:',text)
    if duties:
        text=text[duties.end():]
        text=re.split(r'\n\s*(?:условия|мы предлагаем|что предлагаем|что мы предлагаем)\s*:',text,maxsplit=1)[0]
    snippets=PROFILE.get('cover_letter',{})
    matches=[]
    known_skills=json.dumps(PROFILE['skills'],ensure_ascii=False).lower()
    tasks=[]
    for key,pattern,phrase,evidence_key in (
        ('бизнес-анализ',r'(?:сбор|анализ|уточн|формализ|проработ|разработ|декомпоз)[^\n.]{0,60}требован|бизнес.требован',
         'сборе и формализации требований','requirements'),
        ('BPMN / IDEF0 / IDEF2 / UML',r'(?:описан|моделир|обследован)[^\n.]{0,50}процесс|\bbpmn\b|\bidef[02]\b',
         'описании и моделировании бизнес-процессов','requirements'),
        ('бизнес-анализ',r'техническ[^\n.]{0,30}(?:документ|задан)|аналитическ[^\n.]{0,25}документ|пользовательск[^\n.]{0,20}сценари',
         'подготовке технической документации и пользовательских сценариев','requirements'),
        ('SQL',r'\bsql\b|анализ[^\n.]{0,30}данных|модел[^\n.]{0,20}данных',
         'анализе данных','integration'),
        ('API',r'\bapi\b|интеграц',
         'интеграции систем через API','integration'),
        ('Аналитика и метрики',r'\bkpi\b|\broi\b|retention|churn|удержан|отток|анализ[^\n.]{0,30}метрик|рекламн[^\n.]{0,30}(?:канал|размещ)',
         'анализе метрик и эффективности решений','metrics'),
        ('AI / LLM',r'\bai\b|\bllm\b|нейросет|ии.агент',
         'автоматизации с помощью Python и AI-агентов','automation'),
    ):
        match=re.search(pattern,text)
        if key in PROFILE['skills'] and match:
            tasks.append((match.start(),phrase,evidence_key))
    tasks.sort(key=lambda task:task[0])
    selected_tasks=tasks[:2]
    relevance=''
    if selected_tasks:
        relevance='Могу быть полезен в '+', а также в '.join(task[1] for task in selected_tasks)+'. '
    for key,pattern in (
        ('SQL',r'\bsql\b'),('Python',r'\bpython\b'),('API',r'\bapi\b'),
        ('BPMN',r'\bbpmn\b'),('UML',r'\buml\b'),('IDEF0',r'\bidef0\b'),
        ('IDEF2',r'\bidef2\b'),('Confluence',r'confluence'),('Power BI',r'power\s*bi'),
    ):
        if key.lower() in known_skills and re.search(pattern,text):
            matches.append(key)
    skills='В работе использую '+', '.join(matches[:3])+'. ' if matches else ''
    if not selected_tasks and not skills:
        relevance='Работаю со сбором и формализацией требований, описанием процессов и технической документацией. '
    evidence_key=selected_tasks[0][2] if selected_tasks else 'requirements'
    for preferred in ('metrics','automation'):
        if any(task[2]==preferred for task in selected_tasks):
            evidence_key=preferred
            break
    evidence=snippets.get(evidence_key,PROFILE['project'])
    if not selected_tasks and re.search(r'\bai\b|\bllm\b|автоматизац|нейросет',text):
        evidence=snippets.get('automation',PROFILE['project'])
    elif not selected_tasks and re.search(r'\bkpi\b|\broi\b|retention|churn|удержан|маркетинг',text):
        evidence=snippets.get('metrics',PROFILE['project'])
    elif not selected_tasks and re.search(r'интеграц|\bapi\b|\bpython\b|cad|сапр',text):
        evidence=snippets.get('integration',PROFILE['project'])
    company=clean((v.get('employer') or {}).get('name'))
    destination=f' в «{company}»' if company else ''
    return (f'Здравствуйте! Откликаюсь на вакансию «{clean(v.get("name"))}»{destination}.\n\n'
            f'{snippets.get("intro",PROFILE.get("coordination",""))}\n\n'
            f'{relevance}{skills}{evidence}\n\n'
            f'{snippets.get("closing","Буду рад обсудить задачи на собеседовании.")}\n'
            f'Тимур Хабибуллин')


def hr_decision(messages, settings):
    """No LLM or instructions in vacancy/chat can override these response rules."""
    text='\n'.join(m.get('payload',{}).get('text') or '' for m in messages).strip()
    if any(m.get('payload',{}).get('attachments') for m in messages):
        return 'test','Пришло вложение: изучи задание и согласуй срок',''
    lower=text.lower()
    if '?' not in text and not re.search(r'https?://|пожалуйста|напиш|заполн|выполн|прой[дт]|приглашаем|предлагаем|предложить|\bно\b|\bоднако\b|перей|ссылк',lower):
        if re.fullmatch(r'\s*отказ[.!\s]*',lower) or re.search(r'к сожалению[\s\S]*(?:не готовы|не можем|не подош|не подход|другим кандидат|выбрали другого)',lower):
            return 'info','Отказ работодателя — ответ не требуется',''
        receipt=re.search(r'(?:спасибо|благодарим|благодарю)[\s\S]*(?:за отклик|за интерес|за удел[её]нное время)',lower)
        if receipt and re.search(r'рассмотрим|свяжемся|передам|передадим|удачи|рассмотрен',lower) and not re.search(r'собеседован|интервью|созвон|задани|тест|дедлайн|ответьте',lower):
            return 'info','Уведомление работодателя — ответ не требуется',''
    if re.search(r'собеседован|интервью|созвон|встреч|когда.*удоб|удоб.*когда|сегодня|завтра|\d{1,2}[:.]\d{2}|\d{1,2}\s*(?:ноябр|декабр|январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр)',text,re.I):
        return 'interview','Нужно согласовать время интервью',''
    if re.search(r'тестов|задани|дедлайн|срок|выполн|задач|анкет|опросник|заполн.*форм',text,re.I):
        return 'test','Нужно оценить тестовое и согласовать срок',''
    # Only narrow, single-topic questions get an automatic factual response.
    # A multi-topic or long message is handed to the user with the whole context.
    if len(text)>350 or text.count('?')>1 or re.search(r'https?://|игнорируй|инструкц|перевод|паспорт|карт[аыу]|код из|оферт|договор|соглас[ие]|оплат|отказ|не подош',text,re.I):
        return 'question','Требуется твой ответ',''
    lower=text.lower()
    topics=[]
    if re.search(r'зарплат|заработн|вознагражден|ожидания.*(?:доход|деньг)|финансов.*ожидан',lower):
        topics.append(('salary',f'Здравствуйте! Ориентир по зарплате — {settings["target_salary"]:,} ₽ на руки в месяц. Рассматриваю предложения от {settings["min_salary"]:,} ₽ на руки в зависимости от задач и условий.'.replace(',',' ')))
    if re.search(r'когда.*(?:приступ|выйти|начать)|готов.*(?:приступ|выйти|начать)',lower):
        topics.append(('start','Здравствуйте! Готов приступить к работе как можно раньше.'))
    if re.search(r'(?:какой|какие).*формат|рассматриваете.*(?:офис|гибрид|удален|удалён)',lower):
        topics.append(('format','Здравствуйте! '+PROFILE['formats']))
    # Offers with concrete numbers and unknown extra questions need user input.
    if len(topics)==1 and ('?' in text) and not re.search(r'\d|\bи\b|также|ещ[её]|готовы ли|подходит|устроит|предлагаем|предложить',lower):
        return 'auto',topics[0][0],topics[0][1]
    return 'question','Требуется твой ответ',''


class BrowserBridge:
    def __init__(self,vault=None):
        self.vault=vault
        self.proc=None
        self.lock=threading.Lock()

    def call(self, command):
        with self.lock:
            if not (ROOT/'node_modules'/'playwright').exists():
                raise ValueError('Сначала запусти Установить.cmd для браузерного модуля')
            if not self.proc or self.proc.poll() is not None:
                flags = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
                runtime_path=ROOT/'runtime.json'
                runtime=json.loads(runtime_path.read_text(encoding='utf-8')) if runtime_path.exists() else {}
                node=(runtime.get('node') if os.name=='nt' else None) or shutil.which('node')
                if node and not Path(node).is_absolute():
                    node=str(ROOT/node)
                if not node or not Path(node).exists():
                    raise ValueError('Node.js не найден. Запусти Установить.cmd')
                child_env=os.environ.copy()
                if self.vault and os.environ.get('HH_DATABASE_URL'):
                    state=self.vault.read().get('browser_state')
                    if state:
                        DATA.mkdir(parents=True,exist_ok=True)
                        state_path=DATA/f'.browser-state-{os.getpid()}-{uuid.uuid4().hex}.json'
                        state_path.write_text(json.dumps(state),encoding='utf-8')
                        os.chmod(state_path,0o600)
                        child_env['HH_BROWSER_STATE_FILE']=str(state_path)
                self.proc=subprocess.Popen([node,str(ROOT/'browser.cjs')],cwd=ROOT,stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',creationflags=flags,env=child_env)
            self.proc.stdin.write(json.dumps(command,ensure_ascii=False)+'\n')
            self.proc.stdin.flush()
            line=self.proc.stdout.readline()
            if not line:
                raise ValueError('Браузерный модуль завершился. Открой вход заново.')
            result=json.loads(line)
            if self.vault and os.environ.get('HH_DATABASE_URL') and command.get('action')!='close':
                self.proc.stdin.write(json.dumps({'action':'export_session'})+'\n');self.proc.stdin.flush()
                state=json.loads(self.proc.stdout.readline())
                if state.get('browser_state'):
                    self.vault.update(browser_state=state['browser_state'])
            return result


class Engine:
    def __init__(self, store, vault):
        self.store,self.vault=store,vault
        self.hh=HH(store,vault)
        self.reply_model=ReplyModel(store,vault)
        self.model_working=False
        self.browser=BrowserBridge(vault)
        self.lock=threading.Lock()
        self.wake=threading.Event()
        self.last_search=0
        self.running=False
        self.browser_status={'status':'unverified','logged_in':False,'resumes':[]}
        self.last_chats=0
        self.activity=''
        self.port=8765
        self.oauth_pending={}
        self.oauth_lock=threading.Lock()
        saved_api=self.vault.read()
        self.api_status={'connected':bool(saved_api.get('access_token')),
                         'detail':'API сохранён. Проверка доступа выполняется при запросе.' if saved_api.get('api_client_id') else 'Авторизация API ещё не выполнена.'}
        self.browser_reads=False
        self.captcha_vacancy=None
        self.next_auto_application=0

    def start_oauth(self, client_id, client_secret):
        for value in (client_id,client_secret):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',value):
                raise ValueError('Скопируй Client ID и Client Secret из одобренной заявки.')
        self.vault.update(api_client_id=client_id,client_secret=client_secret)
        state=secrets.token_urlsafe(32)
        with self.oauth_lock:
            self.oauth_pending={state:time.monotonic()+600}
        return 'https://hh.ru/oauth/authorize?'+urllib.parse.urlencode({
            'response_type':'code','client_id':client_id,'state':state,
            'redirect_uri':(os.environ.get('HH_PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL') or f'http://127.0.0.1:{self.port}')+'/oauth/callback'})

    def finish_oauth(self, query):
        state=query.get('state',[''])[0]
        with self.oauth_lock:
            expires=self.oauth_pending.pop(state,0)
        if expires<time.monotonic():
            raise ValueError('Ссылка авторизации истекла или уже использована. Подключи API заново.')
        if query.get('error'):
            raise ValueError('hh.ru не предоставил доступ. Авторизацию можно повторить.')
        code=query.get('code',[''])[0]
        if not code:
            raise ValueError('hh.ru не передал код авторизации.')
        credentials=self.vault.read()
        tokens=self.hh.request('/token',method='POST',form=True,payload={
            'grant_type':'authorization_code','code':code,
            'client_id':credentials['api_client_id'],'client_secret':credentials['client_secret'],
            'redirect_uri':(os.environ.get('HH_PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL') or f'http://127.0.0.1:{self.port}')+'/oauth/callback'})
        self.hh.save_tokens(tokens)
        self.hh.request('/me',auth=True)
        self.api_status={'connected':True,'detail':'OAuth выполнен. Проверка методов соискателя ещё не завершена.'}
        try:
            self.hh.request('/resumes/mine',auth=True)
            self.api_status['detail']='OAuth выполнен. API разрешает чтение резюме соискателя.'
        except HHError:
            self.api_status['detail']='OAuth выполнен. Чтение резюме соискателя через API недоступно.'
        self.hh.request('/vacancies',params={'per_page':1},auth='application')
        self.api_status['detail']+=' Поиск вакансий через API приложения доступен.'
        self.store.event('api',self.api_status['detail'])
        return self.api_status['detail']

    def browser_result(self, command):
        result=self.browser.call(command)
        if result.get('status') in ('error','attention','login_required','uncertain'):
            if result.get('status')=='login_required':
                self.browser_status={'status':'login_required','logged_in':False,'resumes':[]}
            raise BrowserActionError(result.get('detail','Проверь открытую страницу hh.ru'), result.get('status'))
        return result

    def check_session(self):
        self.browser_status=self.browser.call({'action':'session'})
        self.store.event('browser',self.browser_status.get('detail','Проверка браузера выполнена'))
        return self.browser_status

    def read_vacancies(self, path, params=None, brief=None):
        if self.vault.read().get('api_client_id'):
            try:
                return self.hh.request(path,params=params,auth='application')
            except HHError as error:
                if error.code not in (0,500,502,503,504):
                    raise
                time.sleep(2)
                return self.hh.request(path,params=params,auth='application')
        command={'action':'search','params':params} if path=='/vacancies' else {'action':'detail','id':path.split('/')[-1],'brief':brief or {}}
        return self.browser_result(command)

    def chat_read(self, path, params=None):
        command={'action':'chats'} if path=='/common/chats' else {'action':'chat','chat_id':path.split('/')[-2]}
        return self.browser_result(command)

    def search(self, dispatch=False):
        settings=self.store.settings()
        self.activity='Подбор вакансий'
        seen=set()
        count=0
        for query in settings['queries']:
            # Two independent scopes: Moscow all formats + remote across Russia.
            for scope in ({**location,'experience':experience}
                          for experience in ('noExperience','between1And3')
                          for location in ({'area':'1'},{'area':'113','work_format':'REMOTE'})):
                for page in range(10):
                    result=self.read_vacancies('/vacancies',params={'text':query,'search_field':'name','per_page':50,
                                         'page':page,'order_by':'publication_time','period':30,**scope})
                    for brief in result.get('items',[]):
                        vid=str(brief['id'])
                        if vid in seen:
                            continue
                        seen.add(vid)
                        with self.store.connect() as db:
                            existing=db.execute('SELECT status FROM vacancies WHERE id=?',(vid,)).fetchone()
                        if existing and existing[0] in ('applied','already','sending','uncertain','attention','dismissed'):
                            continue
                        # Detail contains actual requirements, not just search snippets.
                        preliminary,_=evaluate(brief,settings)
                        try:
                            vacancy=self.read_vacancies('/vacancies/'+urllib.parse.quote(vid,safe=''),brief=brief) if preliminary>=40 else brief
                        except HHError as error:
                            if error.code!=404:
                                raise
                            vacancy={**brief,'archived':True}
                        score,reason=evaluate(vacancy,settings)
                        status='new' if score>=40 else 'filtered'
                        with self.store.connect() as db:
                            db.execute('''INSERT INTO vacancies(id,body,score,reason,letter,status,updated) VALUES (?,?,?,?,?,?,?)
                              ON CONFLICT(id) DO UPDATE SET body=excluded.body,score=excluded.score,reason=excluded.reason,
                              letter=excluded.letter,status=excluded.status,updated=excluded.updated''',
                              (vid,json.dumps(vacancy,ensure_ascii=False),score,reason,letter_for(vacancy),status,now()))
                        count+=1
                    if dispatch and self.pending_application_count():
                        self.apply_pending()
                        if self.captcha_vacancy:
                            return count
                        self.activity='Подбор вакансий'
                    if page+1>=result.get('pages',1):
                        break
        self.last_search=time.time()
        self.store.event('search',f'Обработано вакансий: {count}. Дубликаты исключены.')
        return count

    def apply_one(self, vid):
        settings=self.store.settings()
        if not settings['resume_id']:
            raise ValueError('Сначала выбери резюме в настройках')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute("SELECT COUNT(*) FROM vacancies WHERE status IN ('applied','sending','uncertain') AND updated>=?",(datetime.now(MSK).date().isoformat(),)).fetchone()[0]
            if count>=settings['daily_limit']:
                raise ValueError('Дневной лимит откликов достигнут')
            row=db.execute('SELECT * FROM vacancies WHERE id=?',(vid,)).fetchone()
            if not row or row['status']!='new':
                raise ValueError('Отклик недоступен или уже обработан')
            if excluded_employer(json.loads(row['body'])):
                db.execute("UPDATE vacancies SET status='filtered',score=0,reason=?,updated=? WHERE id=?",('Aston исключён по твоему пожеланию',now(),vid))
                return {'status':'filtered','detail':'Aston исключён по твоему пожеланию'}
            db.execute("UPDATE vacancies SET status='sending',updated=? WHERE id=?",(now(),vid))
        # Re-check live details immediately before submitting.
        submission_started=False
        try:
            self.activity='Проверка вакансии перед откликом'
            live=self.read_vacancies('/vacancies/'+urllib.parse.quote(vid,safe=''),brief=json.loads(row['body']))
            score,reason=evaluate(live,settings)
            if score<60 or live.get('has_test') or live.get('response_url'):
                result={'status':'attention','pause_required':False,'detail':'Проверь вакансию перед откликом: '+reason}
            else:
                self.activity='Отправка отклика'
                submission_started=True
                result=self.browser.call({'action':'apply','id':vid,'resume_id':settings['resume_id'],'letter':letter_for(live),
                                          'preferences':{'min_salary':settings['min_salary'],'target_salary':settings['target_salary']}})
        except Exception as error:
            # Includes lost IPC after browser click: cannot know whether submitted.
            transient_preflight = not submission_started and ((isinstance(error,HHError) and error.code in (0,404,500,502,503,504)) or bool(re.search(
                r'net::ERR_(?:SSL_PROTOCOL_ERROR|CONNECTION_RESET|TIMED_OUT|CONNECTION_CLOSED)',
                str(error),
            )))
            result={'status':'uncertain' if submission_started else 'attention',
                    'pause_required':not transient_preflight,
                    'detail':('Результат отправки неизвестен. Проверь hh.ru перед повтором. ' if submission_started else 'Не завершена проверка вакансии; отправка не начиналась. ')+str(error).split('\n')[0]}
        with self.store.connect() as db:
            db.execute('UPDATE vacancies SET status=?,updated=? WHERE id=?',(result['status'],now(),vid))
            if result['status']=='applied' and result.get('chat_id'):
                chat={'id':str(result['chat_id']),'vacancy_id':vid,'scope':'assistant','type':'NEGOTIATION'}
                db.execute('INSERT OR REPLACE INTO chats VALUES (?,?,?)',(chat['id'],json.dumps(chat),now()))
        if result.get('captcha'):
            self.captcha_vacancy=vid
            self.store.event('pause', 'Поиск, чаты и отклики ждут прохождения капчи.')
        self.store.event('apply',f'{vid}: {result["detail"]}')
        for answer in result.get('answers',[]):
            self.store.event('questionnaire',f'{vid}: {answer["question"]} → {answer["answer"]}')
        if result['status'] in ('attention','uncertain','error'):
            self.store.task('application',vid,'Нужно проверить отклик',result['detail'])
        return result

    def send(self, chat_id, text, key=None, automated=True):
        if not text.strip() or len(text)>20000:
            raise ValueError('Сообщение должно содержать 1–20 000 символов')
        key=key or str(uuid.uuid4())
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM outbox WHERE chat_id=? AND status IN ('sending','uncertain')",(chat_id,)).fetchone():
                raise BrowserActionError('Сначала проверь доставку предыдущего сообщения и закрой задачу проверки.','attention')
            db.execute('INSERT INTO outbox VALUES (?,?,?,?,?)',(key,chat_id,text,'sending',now()))
        try:
            with self.store.connect() as db:
                last=db.execute('SELECT id FROM messages WHERE chat_id=? ORDER BY CAST(id AS INTEGER) DESC LIMIT 1',(chat_id,)).fetchone()
            if not last:
                raise BrowserActionError('Сначала загрузи переписку этого чата.','attention')
            result=self.browser_result({'action':'send_message','chat_id':str(chat_id),'text':text,'expected_last_id':last[0]})
            if result.get('status')!='sent':
                raise BrowserActionError('Браузер не подтвердил отправку.','uncertain')
            if result.get('message'):
                with self.store.connect() as db:
                    db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?)',
                               (chat_id,str(result['message']['id']),json.dumps(result['message'],ensure_ascii=False),'seen'))
            with self.store.connect() as db:
                db.execute("UPDATE outbox SET status='sent' WHERE id=?",(key,))
            self.store.event('message',f'Сообщение в чат {chat_id} отправлено')
            return result
        except Exception as error:
            uncertain=not isinstance(error,BrowserActionError) or error.status=='uncertain'
            with self.store.connect() as db:
                db.execute('UPDATE outbox SET status=? WHERE id=?',('uncertain' if uncertain else 'blocked',key))
            if uncertain:
                self.store.task('delivery',key,'Проверь доставку сообщения',f'Чат {chat_id}. Результат отправки неизвестен; автоматического повтора нет. Текст: {text}')
            raise

    def queue_hr_review(self, chat, messages, settings, gap=False, allowed=True, refresh_model=False):
        cid=str(chat['id'])
        messages=sorted(messages,key=lambda m:int(m['id']) if str(m['id']).isdigit() else 0)
        last_self=max((i for i,m in enumerate(messages) if m.get('sender_display_info',{}).get('is_current_participant') is True),default=-1)
        tail=[m for m in messages[last_self+1:] if m.get('sender_display_info',{}).get('role')=='EMPLOYER']
        review=draft_reply(tail,PROFILE,settings)
        resume_text=self.store.resume_context()
        model_profile={'resume_text':resume_text,'start':PROFILE['start'],'formats':PROFILE['formats']} if resume_text else PROFILE
        with self.store.connect() as db:
            row=db.execute('SELECT body FROM vacancies WHERE id=?',(str(chat.get('vacancy_id','')),)).fetchone()
        vacancy=json.loads(row[0]) if row else {}
        if tail:
            try:
                used, generated=self.reply_model.review(messages,model_profile,settings,vacancy,refresh=refresh_model)
                if used:
                    review=generated
                elif review:
                    review['source']='rules'
            except ModelError as error:
                self.reply_model.error=str(error)
                if review:
                    review['missing'].insert(0,'API модели недоступен: показан запасной черновик. '+str(error))
                    review['source']='rules'
        else:
            review=None
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute("SELECT * FROM tasks WHERE ref LIKE ? AND kind IN ('question','interview','test') AND status='open' ORDER BY id DESC",(cid+':%',)).fetchall()
            if not review:
                db.execute("UPDATE tasks SET status='done' WHERE ref LIKE ? AND kind IN ('question','interview','test') AND status='open'",(cid+':%',))
                return
            ref=cid+':'+str(tail[-1]['id'])
            handled=db.execute("SELECT 1 FROM tasks WHERE ref=? AND kind IN ('question','interview','test') AND status IN ('done','sending','uncertain')",(ref,)).fetchone()
            if handled:
                for old in existing:
                    db.execute("UPDATE tasks SET status='done' WHERE id=?",(old['id'],))
                return
            vacancy=db.execute('SELECT body FROM vacancies WHERE id=?',(str(chat.get('vacancy_id','')),)).fetchone()
            vacancy=json.loads(vacancy[0]) if vacancy else {}
            review.update(chat_id=cid,source_ref=ref,vacancy_id=str(chat.get('vacancy_id','')),
                          vacancy_title=vacancy.get('name') or (chat.get('display') or {}).get('title') or 'Вопрос по вакансии',
                          employer=(vacancy.get('employer') or {}).get('name',''),
                          checked_at=chat.get('checked_at'),can_send=allowed,last_message_id=str(messages[-1]['id']),
                          messages=[{'id':str(m['id']),'role':m.get('sender_display_info',{}).get('role'),
                                     'text':message_text(m),'at':m.get('creation_time',''),
                                     'attachments':m.get('payload',{}).get('attachments') or []}
                                    for m in messages[-12:] if m.get('sender_display_info',{}).get('role') in ('APPLICANT','EMPLOYER')])
            if gap:
                review['missing'].append('В истории есть разрыв. Проверь контекст переписки.')
            title='Обновился контекст: проверь переписку' if gap else review['vacancy_title']
            if existing:
                task_id=next((old['id'] for old in existing if old['ref']==ref),existing[0]['id'])
                db.execute("UPDATE tasks SET kind='question',ref=?,title=?,body=?,draft=? WHERE id=?",(ref,title,review['body'],review['draft'],task_id))
                for old in existing:
                    if old['id']!=task_id:
                        db.execute("UPDATE tasks SET status='done' WHERE id=?",(old['id'],))
            else:
                db.execute("INSERT INTO tasks(kind,ref,title,body,draft,created) VALUES ('question',?,?,?,?,?)",(ref,title,review['body'],review['draft'],now()))
                task_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
            db.execute('INSERT OR REPLACE INTO hr_reviews VALUES (?,?)',(task_id,json.dumps(review,ensure_ascii=False)))

    def rebuild_hr_reviews(self, refresh_model=False, existing_only=False):
        with self.store.connect() as db:
            chats=[dict(row) for row in db.execute('SELECT * FROM chats')]
            selected={row[0].split(':')[0] for row in db.execute("SELECT ref FROM tasks WHERE status='open' AND kind IN ('question','interview','test')")} if existing_only else None
        owned=self.owned_vacancies()
        for row in chats:
            if selected is not None and row['id'] not in selected:
                continue
            chat=json.loads(row['body'])
            if str(chat.get('vacancy_id','')) not in owned:
                continue
            chat['id']=row['id']
            with self.store.connect() as db:
                messages=[json.loads(m[0]) for m in db.execute('SELECT body FROM messages WHERE chat_id=? ORDER BY CAST(id AS INTEGER) DESC LIMIT 200',(row['id'],))]
            self.queue_hr_review(chat,messages,self.store.settings(),refresh_model=refresh_model)

    def owned_vacancies(self):
        with self.store.connect() as db:
            return {row[0] for row in db.execute("SELECT id FROM vacancies WHERE status='applied'")}

    def sync_chats(self):
        settings=self.store.settings()
        self.activity='Проверка чатов'
        owned=self.owned_vacancies()
        if not owned:
            self.last_chats=time.time()
            self.store.event('chats','Пока нет подтверждённых откликов помощника. Старая переписка не проверяется.')
            return 0
        count=0
        matched_owned=set()
        truncated=False
        for page in range(51):
            result=self.chat_read('/common/chats',params={'page':page,'per_page':20})
            if result.get('truncated'):
                truncated=True
            for chat_index,chat in enumerate(result.get('items',[]),1):
                self.activity=f'Проверка чатов: {chat_index}/{len(result.get("items",[]))}'
                if chat.get('type')!='NEGOTIATION' or chat.get('block_reason'):
                    continue
                cid=str(chat['id'])
                with self.store.connect() as db:
                    known=db.execute('SELECT baseline,body FROM chats WHERE id=?',(cid,)).fetchone()
                if known:
                    saved_chat=json.loads(known['body'])
                    if saved_chat.get('scope') in ('historical','external') and str(saved_chat.get('vacancy_id','')) not in owned:
                        continue
                # Fetch backwards with explicit pagination; never rely on unread count.
                messages=[]
                cursor=None
                exhausted=False
                failed_chat=False
                for _ in range(20):
                    args={'order':'prev','limit':50}
                    if cursor:
                        args['start_message_id']=cursor
                    try:
                        history=self.chat_read('/common/chats/'+urllib.parse.quote(cid,safe='')+'/messages',params=args)
                    except BrowserActionError as error:
                        if error.status=='login_required':
                            raise
                        self.store.task('history',cid,'Нужно открыть чат вручную',str(error))
                        failed_chat=True
                        break
                    batch=history.get('messages',[])
                    fresh=[m for m in batch if str(m['id']) not in {str(x['id']) for x in messages}]
                    messages.extend(fresh)
                    if not fresh or not history.get('has_more'):
                        exhausted=True
                        break
                    oldest=min(batch,key=lambda m:(m.get('creation_time',''),str(m['id'])))
                    cursor=str(oldest['id'])
                    if known:
                        with self.store.connect() as db:
                            if db.execute('SELECT 1 FROM messages WHERE chat_id=? AND id=?',(cid,cursor)).fetchone():
                                exhausted=True
                                break
                if failed_chat:
                    continue
                vacancy_id=str(history.get('vacancy_id') or '')
                chat={**chat,'vacancy_id':vacancy_id,'checked_at':now(),'scope':'assistant' if vacancy_id in owned else 'external'}
                if vacancy_id not in owned:
                    with self.store.connect() as db:
                        db.execute('INSERT OR REPLACE INTO chats VALUES (?,?,?)',(cid,json.dumps(chat,ensure_ascii=False),known[0] if known else now()))
                    continue
                matched_owned.add(vacancy_id)
                if not exhausted:
                    self.store.task('history',cid,'Слишком длинная история чата','Проверь чат вручную: история превышает 1000 сообщений. Автоответ отключён для этого чата.')
                    continue
                with self.store.connect() as db:
                    previous_last=db.execute('SELECT id FROM messages WHERE chat_id=? ORDER BY CAST(id AS INTEGER) DESC LIMIT 1',(cid,)).fetchone()
                gap=bool(known and previous_last and previous_last[0] not in {str(m['id']) for m in messages})
                if any(m.get('_unrecognised') for m in messages):
                    self.store.task('history',cid,'Нужна проверка отправителей','Не удалось определить отправителя одного из сообщений. Автоответ заблокирован.')
                    continue
                messages.sort(key=lambda m:(m.get('creation_time',''),int(m['id']) if str(m['id']).isdigit() else 0))
                incoming=[]
                for m in messages:
                    mid=str(m['id'])
                    with self.store.connect() as db:
                        exists=db.execute('SELECT 1 FROM messages WHERE chat_id=? AND id=?',(cid,mid)).fetchone()
                        if not exists:
                            db.execute('INSERT INTO messages VALUES (?,?,?,?)',(cid,mid,json.dumps(m,ensure_ascii=False),'seen'))
                        else:
                            db.execute('UPDATE messages SET body=? WHERE chat_id=? AND id=?',(json.dumps(m,ensure_ascii=False),cid,mid))
                    sender=m.get('sender_display_info',{})
                    if not exists and sender.get('role')=='EMPLOYER' and sender.get('is_current_participant') is False:
                        incoming.append(m)
                # Only the unanswered tail is actionable, not old questions followed by our replies.
                last_self=max((i for i,m in enumerate(messages) if m.get('sender_display_info',{}).get('is_current_participant') is True),default=-1)
                tail_ids={str(m['id']) for m in messages[last_self+1:]}
                incoming=[m for m in incoming if str(m['id']) in tail_ids]
                with self.store.connect() as db:
                    db.execute("UPDATE tasks SET status='done' WHERE kind='history' AND ref=? AND status='open'",(cid,))
                    db.execute('INSERT OR REPLACE INTO chats VALUES (?,?,?)',(cid,json.dumps(chat,ensure_ascii=False),known[0] if known else now()))
                allowed=history.get('chat_states',{}).get('write_message_state',{}).get('allowed') is True
                self.queue_hr_review(chat,messages,settings,gap=gap,allowed=allowed,refresh_model=True)
                count+=len(incoming)
            if page+1>=result.get('pages',1):
                break
        self.last_chats=time.time()
        if truncated and owned-matched_owned:
            self.store.task('coverage','assistant-chats','Не все новые чаты найдены','Не удалось найти чат по новым откликам помощника: '+', '.join(sorted(owned-matched_owned)))
        else:
            with self.store.connect() as db:
                db.execute("UPDATE tasks SET status='done' WHERE kind='coverage' AND ref='assistant-chats' AND status='open'")
        self.browser_status.update(status='connected',logged_in=True)
        self.store.event('chats',f'Новых входящих сообщений: {count}')
        return count

    def resolve_task(self, task_id, text='', dismiss=False, expected_ref=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task:
                raise ValueError('Задача уже обработана; проверь историю')
            if dismiss:
                if task['status']=='sending':
                    raise ValueError('Дождись завершения отправки')
                if task['kind']=='delivery':
                    db.execute("UPDATE outbox SET status='reviewed' WHERE id=? AND status='uncertain'",(task['ref'],))
                db.execute("UPDATE tasks SET status='done' WHERE id=?",(task_id,))
                return
            if task['status']!='open':
                raise ValueError('Задача уже обработана; проверь историю')
            if task['kind'] not in ('interview','test','question'):
                raise ValueError('Эту задачу нужно проверить в hh.ru и отметить решённой')
            if expected_ref is not None and task['ref']!=expected_ref:
                raise ValueError('HR написал новое сообщение. Обнови карточку и проверь черновик.')
            if re.search(r'\[(?:укажи|проверь|дополни|напиши)[^\]]*\]',text,re.I):
                raise ValueError('Заполни отмеченные места в черновике перед отправкой.')
            context=db.execute('SELECT body FROM hr_reviews WHERE task_id=?',(task_id,)).fetchone()
            if context:
                context=json.loads(context[0])
                latest=db.execute('SELECT id FROM messages WHERE chat_id=? ORDER BY CAST(id AS INTEGER) DESC LIMIT 1',(task['ref'].split(':')[0],)).fetchone()
                if not latest or latest[0]!=context['last_message_id']:
                    raise ValueError('Переписка обновилась. Проверь последние сообщения перед отправкой.')
            db.execute("UPDATE tasks SET status='sending' WHERE id=?",(task_id,))
        try:
            self.send(task['ref'].split(':')[0],text,automated=False)
        except Exception as error:
            status='open' if isinstance(error,BrowserActionError) and error.status!='uncertain' else 'uncertain'
            with self.store.connect() as db:
                db.execute('UPDATE tasks SET status=? WHERE id=?',(status,task_id))
            raise
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET status='done' WHERE id=?",(task_id,))

    def pending_application_count(self):
        settings=self.store.settings()
        if not settings['enabled'] or not settings['auto_apply'] or not settings['resume_id']:
            return 0
        with self.store.connect() as db:
            used=db.execute("SELECT COUNT(*) FROM vacancies WHERE status IN ('applied','sending','uncertain') AND updated>=?",(datetime.now(MSK).date().isoformat(),)).fetchone()[0]
            pending=db.execute("SELECT COUNT(*) FROM vacancies WHERE status='new' AND score>=60").fetchone()[0]
        return max(0,min(pending,settings['daily_limit']-used))

    def apply_pending(self, batch_size=3):
        available=self.pending_application_count()
        if not available:
            return 0
        with self.store.connect() as db:
            rows=db.execute("SELECT id FROM vacancies WHERE status='new' AND score>=60 ORDER BY score DESC,id LIMIT ?",(min(batch_size,available),)).fetchall()
        attempted=0
        for row in rows:
            settings=self.store.settings()
            if not settings['enabled'] or not settings['auto_apply']:
                break
            while time.monotonic()<self.next_auto_application:
                self.activity='Пауза между откликами'
                self.wake.wait(max(0,self.next_auto_application-time.monotonic()))
                self.wake.clear()
                settings=self.store.settings()
                if not settings['enabled'] or not settings['auto_apply']:
                    return attempted
            outcome=self.apply_one(row['id'])
            if outcome['status'] in ('applied','uncertain'):
                self.next_auto_application=time.monotonic()+120
            attempted+=1
            if self.captcha_vacancy:
                break
            if outcome['status'] in ('attention','uncertain','error') and outcome.get('pause_required',True):
                self.store.event('pause','Автоотклики остановлены до проверки браузера. Сними паузу после решения задачи.')
                self.store.save_settings({'auto_apply':False})
                break
        return attempted

    def cycle(self, search_only=False, search_due=True, chat_due=True, dispatch=False):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Операция уже выполняется')
        self.running=True
        try:
            if self.captcha_vacancy:
                return
            settings=self.store.settings()
            if search_only:
                if dispatch:
                    self.store.save_settings({'enabled':True,'auto_apply':True})
                    self.last_search=0
                    if self.apply_pending():
                        return
                    self.search(dispatch=True)
                else:
                    self.search()
                return
            # A populated queue must not wait behind a complete vacancy crawl.
            # Small batches leave checkpoints for chat monitoring and manual actions.
            attempted=self.apply_pending()
            if self.captcha_vacancy:
                return
            if chat_due and (settings['resume_id'] or self.browser_status.get('logged_in')):
                try:
                    self.sync_chats()
                except Exception as error:
                    self.store.event('error',str(error))
                    self.last_chats=time.time()
            else:
                self.last_chats=time.time()
            if attempted or not search_due:
                return
            self.search(dispatch=True)
            if not self.captcha_vacancy:
                self.apply_pending()
        finally:
            self.running=False
            self.activity=''
            self.lock.release()

    def check_captcha(self):
        with self.lock:
            vid=self.captcha_vacancy
            if not vid:
                return
            result=self.browser.call({'action':'captcha_status'})
            if result['status']=='applied':
                with self.store.connect() as db:
                    db.execute("UPDATE vacancies SET status='applied',updated=? WHERE id=? AND status='uncertain'",(now(),vid))
                    db.execute("UPDATE tasks SET status='done' WHERE kind='application' AND ref=? AND status='open'",(vid,))
                self.next_auto_application=time.monotonic()+120
                self.store.event('apply',f'{vid}: hh.ru подтвердил отклик после капчи.')
            elif result['status']=='closed':
                self.store.save_settings({'auto_apply':False})
                self.store.event('pause','Вкладка проверки закрыта. Проверь отклик на hh.ru и включи автоотклики вручную.')
            else:
                return
            self.captcha_vacancy=None

    def loop(self):
        while True:
            if self.captcha_vacancy:
                try:
                    self.check_captcha()
                except Exception as error:
                    self.store.event('error',str(error))
                self.wake.wait(5)
                self.wake.clear()
                continue
            settings=self.store.settings()
            search_due=time.time()-self.last_search>=settings['interval_minutes']*60
            chat_due=time.time()-self.last_chats>=settings['chat_interval_minutes']*60
            applications_due=self.pending_application_count()>0
            if settings['enabled'] and not self.lock.locked() and (applications_due or search_due or chat_due):
                try:
                    self.cycle(search_due=search_due,chat_due=chat_due)
                except Exception as e:
                    self.store.event('error',str(e))
                    if search_due:
                        self.last_search=time.time()
            self.wake.wait(5)
            self.wake.clear()


def make_handler(engine):
    csrf=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # OAuth code and other secrets must not enter access logs.

        def trusted(self):
            return self.headers.get('Host') in (f'127.0.0.1:{engine.port}', f'localhost:{engine.port}',urllib.parse.urlparse(os.environ.get('HH_PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL','')).netloc or '_invalid_')

        def authenticated(self):
            password=os.environ.get('HH_ACCESS_PASSWORD','')
            if not password:
                return True
            expected='Basic '+base64.b64encode(('timur:'+password).encode()).decode()
            if secrets.compare_digest(self.headers.get('Authorization',''),expected):
                return True
            self.send_response(401)
            self.send_header('WWW-Authenticate','Basic realm="Personal assistant"')
            self.send_header('Content-Length','0')
            self.end_headers()
            return False

        def respond(self,status,body,kind='application/json; charset=utf-8'):
            if not isinstance(body,bytes):
                body=json.dumps(body,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path=='/healthz':
                return self.respond(200,{'ok':True})
            if not self.authenticated():
                return
            if not self.trusted():
                return self.respond(403,{'error':'Недопустимый Host'})
            parsed=urllib.parse.urlparse(self.path)
            route=parsed.path
            try:
                if route=='/':
                    return self.respond(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
                if route in ('/app.js','/style.css'):
                    return self.respond(200,(ROOT/route[1:]).read_bytes(),'text/javascript; charset=utf-8' if route.endswith('js') else 'text/css; charset=utf-8')
                if route=='/api/state':
                    state=engine.store.snapshot()
                    state['alerts']=[task for task in state['tasks'] if not task.get('review')]
                    state['tasks']=[task for task in state['tasks'] if task.get('review')]
                    with engine.store.connect() as db:
                        state['counts']={'suitable':db.execute("SELECT COUNT(*) FROM vacancies WHERE score>=60 AND status NOT IN ('filtered','dismissed')").fetchone()[0], 'applied':db.execute("SELECT COUNT(*) FROM vacancies WHERE status='applied'").fetchone()[0]}
                    state['resume_context']=engine.store.resume_context()
                    state['cloud_browser']=os.environ.get('HH_CLOUD_BROWSER')=='1'
                    state.update(model=engine.reply_model.status(),model_working=engine.model_working,csrf=csrf,api=engine.api_status,captcha=engine.captcha_vacancy,running=engine.running or bool(engine.captcha_vacancy),connected=bool(engine.browser_status.get('logged_in')),
                                 browser=engine.browser_status,activity='Ожидаю прохождения капчи' if engine.captcha_vacancy else engine.activity,connection_mode='browser',version='0.2')
                    return self.respond(200,state)
                if route=='/oauth/callback':
                    try:
                        detail=engine.finish_oauth(urllib.parse.parse_qs(parsed.query))
                    except Exception:
                        detail='Авторизация API не завершена. Вернись в помощник и повтори подключение. Коды и ключи скрыты.'
                    page='<meta charset="utf-8"><h1>'+html.escape(detail)+'</h1><a href="/">Вернуться в помощник</a>'
                    return self.respond(200,page.encode('utf-8'),'text/html; charset=utf-8')
                return self.respond(404,{'error':'Не найдено'})
            except Exception as e:
                self.respond(400,{'error':str(e)})

        def do_POST(self):
            if not self.authenticated():
                return
            origin=self.headers.get('Origin')
            if not self.trusted() or self.headers.get('X-CSRF-Token')!=csrf or origin not in (None,f'http://127.0.0.1:{engine.port}',f'http://localhost:{engine.port}',(os.environ.get('HH_PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL','_invalid_'))):
                return self.respond(403,{'error':'Недопустимый источник запроса'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=100000:
                    raise ValueError('Недопустимый размер запроса')
                payload=json.loads(self.rfile.read(length))
                route=urllib.parse.urlparse(self.path).path
                if engine.captcha_vacancy and route in ('/api/search','/api/login','/api/open-chats','/api/session','/api/chats','/api/apply','/api/task') and not (route=='/api/task' and payload.get('dismiss')):
                    raise ValueError('Пройди капчу в открытой вкладке hh.ru.')
                if route=='/api/oauth':
                    url=engine.start_oauth(payload.get('client_id'),payload.get('client_secret'))
                    return self.respond(200,{'url':url})
                elif route=='/api/resume-context':
                    text=payload.get('text','')
                    if not isinstance(text,str) or len(text)>40000:
                        raise ValueError('Текст резюме должен быть короче 40 000 символов')
                    with engine.store.connect() as db:
                        db.execute("INSERT OR REPLACE INTO documents VALUES ('resume',?)",(text.strip(),))
                elif route=='/api/model':
                    engine.reply_model.configure(payload)
                    return self.respond(200,engine.reply_model.status())
                elif route=='/api/model/test':
                    answer=engine.reply_model.request([{'role':'user','content':'Верни JSON: needs_reply=false, draft="", missing=[], reason="Проверка подключения"'}],engine.store.settings())
                    engine.reply_model.error=''
                    return self.respond(200,{'ok':True})
                elif route=='/api/drafts':
                    if not engine.store.settings().get('model_enabled'):
                        raise ValueError('Подключи API модели в настройках')
                    if engine.model_working or not engine.lock.acquire(blocking=False):
                        raise ValueError('Сейчас выполняется другая операция. Повтори после её завершения.')
                    engine.model_working=True
                    def generate_drafts():
                        try:
                            engine.rebuild_hr_reviews(refresh_model=True,existing_only=True)
                        except Exception:
                            engine.reply_model.error='Не удалось обновить все черновики. Повтори запрос.'
                        finally:
                            engine.model_working=False
                            engine.lock.release()
                    threading.Thread(target=generate_drafts,daemon=True).start()
                elif route=='/api/settings':
                    if 'client_secret' in payload or 'client_id' in payload:
                        raise ValueError('Ключи API больше не нужны. Используй вход в браузере.')
                    engine.store.save_settings(payload)
                    engine.wake.set()
                elif route=='/api/search':
                    if engine.running:
                        raise ValueError('Операция уже выполняется')
                    def search_job():
                        try:
                            engine.cycle(search_only=True,dispatch=payload.get('dispatch') is True)
                        except Exception as e:
                            engine.store.event('error',str(e))
                    threading.Thread(target=search_job,daemon=True).start()
                elif route=='/api/login':
                    return self.respond(200,engine.browser.call({'action':'login'}))
                elif route=='/api/open-chats':
                    return self.respond(200,engine.browser.call({'action':'open_chats'}))
                elif route=='/api/session':
                    if not engine.lock.acquire(blocking=False):
                        raise ValueError('Дождись завершения операции')
                    try:
                        return self.respond(200,engine.check_session())
                    finally:
                        engine.lock.release()
                elif route=='/api/chats':
                    if not engine.lock.acquire(blocking=False):
                        raise ValueError('Браузер занят: '+(engine.activity or 'выполняется операция')+'. Проверка чатов будет доступна после её завершения.')
                    try:
                        engine.running=True
                        engine.sync_chats()
                    finally:
                        engine.running=False
                        engine.activity=''
                        engine.lock.release()
                elif route=='/api/task':
                    if bool(payload.get('dismiss')):
                        # Closing a local task uses a DB transaction, not the browser.
                        # resolve_task still refuses to close a task being sent.
                        engine.resolve_task(int(payload['id']),dismiss=True)
                        return self.respond(200,{'ok':True})
                    if not engine.lock.acquire(blocking=False):
                        raise ValueError('Браузер занят: '+(engine.activity or 'выполняется операция')+'. Отправить ответ можно после её завершения.')
                    try:
                        engine.resolve_task(int(payload['id']),str(payload.get('text','')),bool(payload.get('dismiss')),expected_ref=payload.get('expected_ref'))
                    finally:
                        engine.lock.release()
                elif route=='/api/apply':
                    if not engine.lock.acquire(blocking=False):
                        raise ValueError('Браузер занят: '+(engine.activity or 'выполняется операция'))
                    try:
                        engine.running=True
                        engine.activity='Отправка отклика'
                        return self.respond(200,engine.apply_one(str(payload['id'])))
                    finally:
                        engine.running=False
                        engine.activity=''
                        engine.lock.release()
                elif route=='/api/dismiss':
                    with engine.store.connect() as db:
                        db.execute("UPDATE vacancies SET status='dismissed' WHERE id=? AND status IN ('new','filtered')",(str(payload['id']),))
                elif route=='/api/close':
                    engine.store.save_settings({'enabled':False})
                    engine.wake.set()
                    def stop():
                        # No force kill while a submission is in flight.
                        with engine.lock:
                            engine.browser.call({'action':'close'}) if engine.browser.proc else None
                            self.server.shutdown()
                    threading.Thread(target=stop,daemon=True).start()
                else:
                    return self.respond(404,{'error':'Не найдено'})
                return self.respond(200,{'ok':True})
            except Exception as e:
                self.respond(400,{'error':str(e)})
    return Handler


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','8765')))
    parser.add_argument('--host',default=os.environ.get('HH_BIND_HOST','127.0.0.1'))
    parser.add_argument('--no-open',action='store_true')
    args=parser.parse_args()
    if args.host not in ('127.0.0.1','localhost') and not os.environ.get('HH_ACCESS_PASSWORD'):
        raise RuntimeError('Для внешнего доступа задай HH_ACCESS_PASSWORD и HTTPS-прокси')
    store=Store()
    engine=Engine(store,Vault())
    if not os.environ.get('HH_DATABASE_URL'):
        engine.rebuild_hr_reviews()
    engine.port=args.port
    try:
        server=ThreadingHTTPServer((args.host,args.port),make_handler(engine))
    except OSError:
        if not args.no_open:
            webbrowser.open(f'http://127.0.0.1:{args.port}')
        print('Порт занят. Возможно, приложение уже запущено.')
        return
    threading.Thread(target=engine.loop,daemon=True).start()
    print(f'HH помощник: http://127.0.0.1:{args.port}')
    if not args.no_open:
        webbrowser.open(f'http://127.0.0.1:{args.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=='__main__':
    main()
