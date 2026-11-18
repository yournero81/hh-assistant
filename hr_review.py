"""Grounded HR reply drafts. All drafts require the applicant's review."""
import re


REJECTION = re.compile(
    r'не (?:готовы|можем|сможем)\s+(?:вас\s+)?приглас|'
    r'отдали предпочтение|выбрали другого|кандидатура.*отклонена|'
    r'вакансия (?:уже )?закрыта|^\s*отказ[.!\s]*$', re.I)
REQUEST = re.compile(
    r'\?|расскажите|уточните|подскажите|ответьте|напишите|пришлите|'
    r'укажите|заполните|пройдите|выполните|выберите|подтвердите|'
    r'приглашаем|предлагаем.*(?:интервью|собеседован|созвон)|тестовое задание', re.I)


def message_text(message):
    return (message.get('payload', {}).get('text') or '').strip()


def needs_reply(text):
    if REJECTION.search(text) and '?' not in text:
        return False
    if re.search(r'В разговоре с кандидатом я узнал|Ваши ответы отправлены работодателю|Вопросы к работодателю, на которые помощник', text, re.I):
        return False
    if re.fullmatch(r'[^\n]+(?:\?|:)\s*:\s*[^\n]+', text.strip()):
        return False  # Completed application questionnaire, not a new HR question.
    text=re.sub(r'(?im)^\s*что дальше\?\s*$', '', text)
    # Query parameters in links are not questions.
    return bool(REQUEST.search(re.sub(r'https?://\S+', '', text)))


TECHNICAL_ANSWERS = (
    (r'\bjson\b', 'JSON описывает данные через объекты, массивы и значения; при обмене через API важны типы полей, обязательность и вложенность.'),
    (r'\bxml\b', 'XML представляет данные деревом элементов и атрибутов; структуру и типы можно проверять по XSD.'),
    (r'\bc4\b', 'C4 описывает архитектуру на уровнях контекста, контейнеров, компонентов и кода.'),
    (r'kafka|rabbitmq|брокер.*сообщ|микросервис', 'В микросервисной архитектуре сервисы разделены по ответственности. Брокеры обеспечивают асинхронный обмен: Kafka хранит поток событий, RabbitMQ маршрутизирует сообщения через обменники и очереди. Важны повторная доставка, идемпотентность и обработка ошибок.'),
    (r'\brest\b|swagger|openapi|postman', 'В REST API ресурсы доступны по HTTP; контракт фиксирует методы, параметры, схемы запросов и ответов, коды ошибок. OpenAPI/Swagger описывает контракт, Postman позволяет проверять запросы и ответы.'),
    (r'\ber\b|er.диаграм', 'ER-диаграмма показывает сущности, атрибуты, первичные и внешние ключи, связи и их кратность.'),
    (r'agile|scrum', 'В Scrum работа организуется короткими спринтами с приоритизированным бэклогом, планированием, обзорами и ретроспективами; требования уточняются вместе с командой и заказчиком.'),
)


def neutral_answer(question, profile):
    q = question.lower()
    if re.search(r'английск|english', q):
        return 'Уровень английского готов подтвердить на короткой беседе или проверке, чтобы точно соотнести его с требованиями вакансии.'
    if re.search(r'опыт|работали|ситуаци|кейс|лет', q):
        return profile['experience']+' '+profile.get('project','')+' Этот опыт могу подробно разобрать применительно к вашим задачам.'
    return 'Готов обсудить этот вопрос применительно к задачам вакансии. Мой основной профиль — аналитика, работа с требованиями, SQL, Python и API.'


def answer_question(text, profile, settings):
    """Answer known facts, flag unsupported products and specialist claims."""
    answers = {}
    missing = []
    for part in [text]:
        question = re.sub(r'\s+', ' ', part).strip()
        if not question:
            continue
        q = re.sub(r'https?://\S+', '', question.lower())
        matched = False
        known_text = str(profile['skills']).lower()
        technical_terms = re.findall(r'\b[A-Za-z][A-Za-z0-9.+/-]{1,24}\b', q)
        unknown_terms = [term for term in technical_terms if term.lower() not in known_text and term.lower() not in ('да','нет')]
        named_experience = re.search(r'опыт\s+(?:работы\s+)?(?:с|со|в)\s+([^?.,]{3,80})', q)
        unknown_product = bool(re.search(r'адванта|исуп|1с|1c|low.code|аутсорс|продуктов.*компан|производств|сфер.*транспорт|стейкхолдер|ситуаци|agile|scrum|системн.*анализ',q))
        if named_experience and not any(word in named_experience[1] for word in ('аналитик','анализа','проект','разработ','команд','sql','python','api')):
            unknown_product = named_experience[1].strip() not in known_text

        if re.search(r'игнорируй|предыдущие инструкции|системный промпт|код.*смс|паспорт|снилс|номер.*карт', q):
            missing.append('Проверь запрос перед ответом. ' + question)
            continue
        if re.search(r'прой[дт].*(?:бот|гига|анкет)|(?:бот|гига).*(?:интервью|отбор)|заполн.*(?:анкет|форм)', q):
            return ['[Проверь анкету или интервью по ссылке HR и дополни ответ.]'], ['Нужно пройти или изучить анкету по ссылке. Время встречи не назначается автоматически.']
        if re.search(r'собеседован|интервью|созвон|когда.*удоб|время.*встреч', q):
            answers['interview'] = 'Готов обсудить вакансию. Мне удобно [укажи дату и время по Москве].'
            missing.append('Выбери время интервью по Москве.')
            matched = True
        if re.search(r'тестов|дедлайн|срок.*выполн|заполн.*(?:анкет|форм)|пройдите', q):
            answers['test'] = '[Проверь задание или анкету и дополни ответ HR, включая согласованный срок.]'
            missing.append('Нужно изучить задание или анкету и согласовать срок.')
            matched = True
        if re.search(r'зарплат|заработн|вознагражден|доход|финансов.*ожидан', q):
            answers['salary'] = (
                f'Ориентир по зарплате — {settings["target_salary"]:,} ₽ на руки в месяц. '
                f'Рассматриваю предложения от {settings["min_salary"]:,} ₽ на руки в зависимости от задач и условий.'
            ).replace(',', ' ')
            matched = True
        if re.search(r'когда.*(?:приступ|выйти|начать)|готов.*(?:приступ|выйти|начать)|дата.*выход', q):
            answers['start'] = profile['start']
            matched = True
        if re.search(r'формат.*работ|офис|гибрид|удал[её]н', q):
            answers['format'] = profile['formats']
            matched = True
        if re.search(r'город|где.*(?:жив|прожив)|место.*прожив', q):
            answers['city'] = 'Проживаю в ' + ('Москве.' if profile['city'] == 'Москва' else profile['city'] + '.')
            matched = True
        if re.search(r'образован|магистрат|бакалавр|университет|вуз|где.*учит', q):
            answers['education'] = profile['education']
            matched = True
        if re.search(r'командиров', q):
            answers['travel'] = profile['travel']
            matched = True
        if not unknown_product and re.search(r'опыт|работали|занимались|обязанност|достижен|расскажите о себе', q):
            answers['experience'] = profile['experience']
            matched = True
        if re.search(r'автоматизац|ai.агент|ии.агент|расскажите.*проект|кейс|интеграцион.*взаим|сложн.*систем.*интеграц', q) and not re.search(r'тестов|выполните|стейкхолдер|ситуаци', q):
            key = 'automation_project' if re.search(r'ai|ии|видео|llm|автоматизац', q) else 'project'
            answers[key] = profile.get(key, profile['project'])
            matched = True
        for key, pattern in (
            ('SQL', r'\bsql\b'), ('Python', r'\bpython\b'), ('API', r'\bapi\b'),
            ('Power BI', r'power\s*bi'), ('Confluence', r'confluence'),
            ('BPMN / IDEF0 / IDEF2 / UML', r'bpmn|uml|idef|sequence'),
        ):
            if re.search(pattern, q) and key in profile['skills']:
                answers[key] = key + ': ' + profile['skills'][key] + '.'
                matched = True
        if re.search(r'документаци|\bтз\b|user stor',q):
            answers['documentation'] = 'Мои компетенции — '+profile['skills'].get('бизнес-анализ','подготовка технической документации')+'.'
            matched = True
        if re.search(r'какие инструменты|каких нотациях',q):
            answers['tools'] = 'Использую BPMN 2.0, IDEF0, IDEF2, UML и sequence-диаграммы. Инструменты — Confluence, AllFusion, Power BI и Git.'
            matched = True
        for pattern, explanation in TECHNICAL_ANSWERS:
            if re.search(pattern, q):
                answers[pattern] = explanation
                matched = True
        unknown = re.search(r'английск|english|1с|1c|postgres|hadoop|java\b|relocat|релокац|гражданств|разрешени.*работ|полный.*день|\b40\s*час', q)
        if unknown or unknown_product or unknown_terms:
            missing.append('Практический опыт или личные сведения по этому пункту не подтверждены резюме; черновик не заявляет их наличие. ' + question)
        elif not matched and (REQUEST.search(question) or re.match(r'^\d+[.)]',question)):
            missing.append('Дополни ответ на вопрос. ' + question)
    return list(dict.fromkeys(answers.values())), list(dict.fromkeys(missing))


def draft_reply(messages, profile, settings):
    """Return an actionable unanswered HR tail and a factual editable draft."""
    incoming = [m for m in messages if m.get('sender_display_info', {}).get('role') == 'EMPLOYER']
    if not incoming:
        return None
    latest = message_text(incoming[-1])
    if REJECTION.search(latest) and '?' not in latest:
        return None
    actionable = [m for m in incoming if needs_reply(message_text(m)) or
                  (not message_text(m) and m.get('payload', {}).get('attachments'))]
    if not actionable:
        return None
    texts = list(dict.fromkeys(message_text(m) for m in actionable if message_text(m)))
    text = '\n\n'.join(texts)
    # Remove signatures and acknowledgement footer from the last checklist item.
    question_text = re.split(r'(?im)^\s*(?:Мы (?:внимательно изучим|рассмотрим)|В случае заинтересованности|С уважением|Обратите внимание:|Вы также можете узнать|Узнать больше о работе|Команда Центра|Спасибо за Ваше желание)', text)[0]
    question_text = re.sub(r'https?://\S+', '', question_text)
    normalized = re.sub(r'\n{2,}', '\n', question_text)
    numbered = list(re.finditer(r'(?m)^\s*\d{1,2}[.)]\s+', normalized))
    if numbered:
        questions = [normalized[match.start():numbered[index+1].start() if index+1<len(numbered) else len(normalized)].strip()
                     for index,match in enumerate(numbered)]
    else:
        questions = [part.strip() for part in re.split(r'(?<=\?)\s*|\n+', normalized) if part.strip()]
    questions = [q for q in questions if REQUEST.search(q) or re.match(r'^\d+[.)]',q)]
    paragraphs = []
    missing = []
    for index,question in enumerate(questions,1):
        if not (REQUEST.search(question) or re.match(r'^\d+[.)]',question)):
            continue
        answers, notes = answer_question(question,profile,settings)
        if notes:
            missing.extend(notes)
            # Unknown facts are review notes, not blanks in a draft.
            # Only actual commitments (dates/deadlines) keep explicit placeholders.
        if not answers:
            answers = [neutral_answer(question, profile)]
        label = re.match(r'^(\d+)[.)]',question)
        prefix = (label[1] if label else str(index))+'. ' if len(questions)>1 else ''
        paragraphs.append(prefix+' '.join(answers))
    if any(m.get('payload', {}).get('attachments') for m in actionable):
        missing.append('Проверь вложения или ссылки из сообщения HR.')
    if not paragraphs:
        paragraphs.append('[Проверь сообщение или вложение и дополни ответ HR.]')
    return {'draft':'Здравствуйте!\n\n'+'\n\n'.join(paragraphs),
            'missing':list(dict.fromkeys(missing)), 'body':text}
