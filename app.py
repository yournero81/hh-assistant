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

ROOT = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
DATA = ROOT / 'data'
MSK = timezone(timedelta(hours=3))
DEFAULTS = dict(min_salary=60000, target_salary=70000, interval_minutes=30,
                daily_limit=15, resume_id='', user_agent='TimurJobAssistant/0.2', chat_interval_minutes=5,
                auto_apply=False, auto_reply=False, enabled=False,
                queries=['бизнес-аналитик', 'системный аналитик', 'аналитик данных', 'BI аналитик', 'аналитик бизнес-процессов'])
PROFILE = json.loads((ROOT / ('profile.json' if (ROOT/'profile.json').exists() else 'profile.example.json')).read_text(encoding='utf-8'))


def now():
    return datetime.now(MSK).isoformat(timespec='seconds')


def clean(value):
    return html.unescape(re.sub('<[^>]+>', ' ', str(value or ''))).strip()


class Store:
    def __init__(self, folder=DATA):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = self.folder / 'assistant.sqlite3'
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
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

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
            if key in ('enabled', 'auto_apply', 'auto_reply'):
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
            return dict(settings=self.settings(), vacancies=vacancies,
                        tasks=[dict(r) for r in db.execute("SELECT * FROM tasks WHERE status!='done' ORDER BY id DESC LIMIT 200")],
                        events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')],
                        outbox=[dict(r) for r in db.execute('SELECT * FROM outbox ORDER BY created DESC LIMIT 50')])


class Vault:
    """DPAPI binds secrets to the current Windows login. Never returned by HTTP."""
    def __init__(self, folder=DATA):
        self.path = Path(folder) / 'secrets.dpapi'
        self.lock = threading.RLock()

    @staticmethod
    def crypt(data, decrypt=False):
        if os.name != 'nt':
            raise RuntimeError('Хранилище ключей поддерживается только в Windows')
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
            if not self.path.exists():
                return {}
            return json.loads(self.crypt(self.path.read_bytes(),True))

    def update(self, **values):
        with self.lock:
            current = self.read()
            current.update(values)
            blob = self.crypt(json.dumps(current).encode())
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
        headers = {'User-Agent':self.store.settings()['user_agent'], 'Accept':'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer '+self.token()
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
                'client_id':self.store.settings()['client_id'],'client_secret':sec.get('client_secret','')},form=True)
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
    if experience!='noExperience':
        return 0,'Только вакансии «Без опыта»: опыт требуется или не указан однозначно'
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
    score+=15; reasons.append('Без опыта работы')
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
    text=clean(v.get('description') or json.dumps(v.get('snippet',{}),ensure_ascii=False)).lower()
    entries=[]
    triggers={'SQL':r'\bsql\b','Python':r'\bpython\b','Power BI':r'power\s*bi','Excel':r'excel','бизнес-анализ':r'бизнес|требован|процесс|документац'}
    for key,pattern in triggers.items():
        if re.search(pattern,text):
            entries.append(f'{key}: {PROFILE["skills"][key]}')
    focus='; '.join(entries[:3]) if entries else 'SQL, Python, Power BI и Excel, описание бизнес-процессов и структурирование требований'
    return (f'Здравствуйте! Меня заинтересовала вакансия «{clean(v.get("name"))}» в компании «{clean((v.get("employer") or {}).get("name"))}».\n\n'
            f'{PROFILE["education"]}\nВладею следующими навыками: {focus}.\n'
            f'{PROFILE["project"]}\n\n'
            'Рассматриваю стартовую позицию в аналитике и готов развиваться в задачах вашей команды. '
            f'{PROFILE["start"]} Буду рад обсудить вакансию.\n\nС уважением, {PROFILE["name"]}')


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
    def __init__(self):
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
                node=runtime.get('node') or shutil.which('node')
                if node and not Path(node).is_absolute():
                    node=str(ROOT/node)
                if not node or not Path(node).exists():
                    raise ValueError('Node.js не найден. Запусти Установить.cmd')
                self.proc=subprocess.Popen([node,str(ROOT/'browser.cjs')],cwd=ROOT,stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',creationflags=flags)
            self.proc.stdin.write(json.dumps(command,ensure_ascii=False)+'\n')
            self.proc.stdin.flush()
            line=self.proc.stdout.readline()
            if not line:
                raise ValueError('Браузерный модуль завершился. Открой вход заново.')
            return json.loads(line)


class Engine:
    def __init__(self, store, vault):
        self.store,self.vault=store,vault
        self.hh=HH(store,vault)
        self.browser=BrowserBridge()
        self.lock=threading.Lock()
        self.wake=threading.Event()
        self.last_search=0
        self.running=False
        self.browser_status={'status':'unverified','logged_in':False,'resumes':[]}
        self.last_chats=0
        self.activity=''
        self.port=8765
        self.browser_reads=False

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
        command={'action':'search','params':params} if path=='/vacancies' else {'action':'detail','id':path.split('/')[-1],'brief':brief or {}}
        return self.browser_result(command)

    def chat_read(self, path, params=None):
        command={'action':'chats'} if path=='/common/chats' else {'action':'chat','chat_id':path.split('/')[-2]}
        return self.browser_result(command)

    def search(self):
        settings=self.store.settings()
        self.activity='Подбор вакансий'
        seen=set()
        count=0
        for query in settings['queries']:
            # Two independent scopes: Moscow all formats + remote across Russia.
            for scope in ({'area':'1'},{'area':'113','work_format':'REMOTE'}):
                for page in range(2):
                    result=self.read_vacancies('/vacancies',params={'text':query,'search_field':'name','experience':'noExperience','per_page':50,
                                         'page':page,'order_by':'publication_time','period':7,**scope})
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
                        vacancy=self.read_vacancies('/vacancies/'+urllib.parse.quote(vid,safe=''),brief=brief) if preliminary>=40 else brief
                        score,reason=evaluate(vacancy,settings)
                        status='new' if score>=40 else 'filtered'
                        with self.store.connect() as db:
                            db.execute('''INSERT INTO vacancies(id,body,score,reason,letter,status,updated) VALUES (?,?,?,?,?,?,?)
                              ON CONFLICT(id) DO UPDATE SET body=excluded.body,score=excluded.score,reason=excluded.reason,
                              letter=excluded.letter,status=excluded.status,updated=excluded.updated''',
                              (vid,json.dumps(vacancy,ensure_ascii=False),score,reason,letter_for(vacancy),status,now()))
                        count+=1
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
                result=self.browser.call({'action':'apply','id':vid,'resume_id':settings['resume_id'],'letter':row['letter'],
                                          'preferences':{'min_salary':settings['min_salary'],'target_salary':settings['target_salary']}})
        except Exception as error:
            # Includes lost IPC after browser click: cannot know whether submitted.
            result={'status':'uncertain' if submission_started else 'attention',
                    'detail':('Результат отправки неизвестен. Проверь hh.ru перед повтором. ' if submission_started else 'Не завершена проверка вакансии; отправка не начиналась. ')+str(error).split('\n')[0]}
        with self.store.connect() as db:
            db.execute('UPDATE vacancies SET status=?,updated=? WHERE id=?',(result['status'],now(),vid))
            if result['status']=='applied' and result.get('chat_id'):
                chat={'id':str(result['chat_id']),'vacancy_id':vid,'scope':'assistant','type':'NEGOTIATION'}
                db.execute('INSERT OR REPLACE INTO chats VALUES (?,?,?)',(chat['id'],json.dumps(chat),now()))
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
                chat={**chat,'vacancy_id':vacancy_id,'scope':'assistant' if vacancy_id in owned else 'external'}
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
                full_tail=[m for m in messages[last_self+1:] if m.get('sender_display_info',{}).get('role')=='EMPLOYER']
                if full_tail:
                    existing_kind,_,existing_draft=hr_decision(full_tail,settings)
                    body='\n\n'.join((m.get('payload',{}).get('text') or '')+
                                     ''.join('\nВложение или ссылка: '+str(a.get('title','Файл')) for a in m.get('payload',{}).get('attachments') or []) for m in full_tail)
                    with self.store.connect() as db:
                        if existing_kind=='info':
                            db.execute("UPDATE tasks SET status='done' WHERE ref LIKE ? AND status='open' AND title='Первое подключение: проверь переписку'",(cid+':%',))
                        else:
                            db.execute("UPDATE tasks SET body=?,draft=? WHERE ref LIKE ? AND status='open' AND title='Первое подключение: проверь переписку'",(body,existing_draft,cid+':%'))
                with self.store.connect() as db:
                    db.execute("UPDATE tasks SET status='done' WHERE kind='history' AND ref=? AND status='open'",(cid,))
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO chats VALUES (?,?,?)',(cid,json.dumps(chat,ensure_ascii=False),known[0] if known else now()))
                if not incoming:
                    continue
                count+=len(incoming)
                text='\n\n'.join((m.get('payload',{}).get('text') or '')+
                                 ''.join('\nВложение: '+str(a.get('title','Файл')) for a in m.get('payload',{}).get('attachments') or [])
                                 for m in incoming)
                kind,title,draft=hr_decision(incoming,settings)
                if kind=='info':
                    continue
                ref=cid+':'+str(incoming[-1]['id'])
                # Existing backlog is queued for review instead of replying to stale messages.
                allowed=history.get('chat_states',{}).get('write_message_state',{}).get('allowed') is True
                with self.store.connect() as db:
                    pending=db.execute("SELECT 1 FROM tasks WHERE ref LIKE ? AND status IN ('open','sending','uncertain') AND kind IN ('interview','test','question','salary','start','format')",(cid+':%',)).fetchone()
                    uncertain_delivery=db.execute("SELECT 1 FROM outbox WHERE chat_id=? AND status IN ('sending','uncertain')",(cid,)).fetchone()
                current=self.store.settings()
                if not gap and kind=='auto' and current['enabled'] and current['auto_reply'] and allowed and not pending and not uncertain_delivery:
                    try:
                        self.send(cid,draft)
                    except Exception:
                        self.store.task('question',ref,'Автоответ не отправлен',text,draft)
                        raise
                else:
                    self.store.task(kind if kind!='auto' else 'question',ref,
                                    ('Обновился контекст: проверь переписку' if gap else title),text,draft)
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

    def resolve_task(self, task_id, text='', dismiss=False):
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
            outcome=self.apply_one(row['id'])
            attempted+=1
            if outcome['status'] in ('attention','uncertain','error') and outcome.get('pause_required',True):
                self.store.event('pause','Автоотклики остановлены до проверки браузера. Сними паузу после решения задачи.')
                self.store.save_settings({'auto_apply':False})
                break
            if attempted<len(rows):
                self.activity='Пауза между откликами'
                self.wake.wait(30 if outcome['status']=='applied' else 5)
                self.wake.clear()
        return attempted

    def cycle(self, search_only=False, search_due=True, chat_due=True):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Операция уже выполняется')
        self.running=True
        try:
            settings=self.store.settings()
            if search_only:
                self.search()
                return
            # A populated queue must not wait behind a complete vacancy crawl.
            # Small batches leave checkpoints for chat monitoring and manual actions.
            attempted=self.apply_pending()
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
            self.search()
            self.apply_pending()
        finally:
            self.running=False
            self.activity=''
            self.lock.release()

    def loop(self):
        while True:
            settings=self.store.settings()
            search_due=time.time()-self.last_search>=settings['interval_minutes']*60
            chat_due=time.time()-self.last_chats>=settings['chat_interval_minutes']*60
            applications_due=self.pending_application_count()>0
            if settings['enabled'] and (applications_due or search_due or chat_due):
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
            return self.headers.get('Host') in (f'127.0.0.1:{engine.port}', f'localhost:{engine.port}')

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
                    state.update(csrf=csrf,running=engine.running,connected=bool(engine.browser_status.get('logged_in')),
                                 browser=engine.browser_status,activity=engine.activity,connection_mode='browser',version='0.2')
                    return self.respond(200,state)
                if route in ('/oauth/start','/oauth/callback'):
                    return self.respond(410,{'error':'API для соискателей отключён hh.ru. Используй вход через браузер в разделе «Подключение».'})
                return self.respond(404,{'error':'Не найдено'})
            except Exception as e:
                self.respond(400,{'error':str(e)})

        def do_POST(self):
            origin=self.headers.get('Origin')
            if not self.trusted() or self.headers.get('X-CSRF-Token')!=csrf or origin not in (None,f'http://127.0.0.1:{engine.port}',f'http://localhost:{engine.port}'):
                return self.respond(403,{'error':'Недопустимый источник запроса'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=100000:
                    raise ValueError('Недопустимый размер запроса')
                payload=json.loads(self.rfile.read(length))
                route=urllib.parse.urlparse(self.path).path
                if route=='/api/settings':
                    if 'client_secret' in payload or 'client_id' in payload:
                        raise ValueError('Ключи API больше не нужны. Используй вход в браузере.')
                    engine.store.save_settings(payload)
                    engine.wake.set()
                elif route=='/api/search':
                    if engine.running:
                        raise ValueError('Операция уже выполняется')
                    def search_job():
                        try:
                            engine.cycle(search_only=True)
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
                        engine.resolve_task(int(payload['id']),str(payload.get('text','')),bool(payload.get('dismiss')))
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
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--no-open',action='store_true')
    args=parser.parse_args()
    store=Store()
    engine=Engine(store,Vault())
    engine.port=args.port
    try:
        server=ThreadingHTTPServer(('127.0.0.1',args.port),make_handler(engine))
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
