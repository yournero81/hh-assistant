let state,csrf,loadedForm=false,activeTab='overview',lastEvent=0,chatSyncInFlight=false;
const taskDrafts=new Map();
const $=s=>document.querySelector(s);
const labels={new:'В подборке',filtered:'Отсеяна',applied:'Отклик отправлен',already:'Уже есть отклик',attention:'Нужна проверка',uncertain:'Результат неизвестен',sending:'Отправка',dismissed:'Скрыта',error:'Ошибка',open:'Ожидает ответа',done:'Решено',sent:'Подтверждено на сайте',blocked:'Не отправлено',reviewed:'Проверено вручную'};
const tabs={overview:'Обзор',tasks:'Нужен твой ответ',settings:'Настройки'};
function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function toast(text,error=false){$('#toast').textContent=text;$('#toast').className=error?'error':'';$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,6500);}
async function api(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body||{})});const data=await r.json();if(!r.ok)throw Error(data.error||'Ошибка запроса');return data;}
async function action(fn){try{await fn();await refresh();}catch(e){toast(e.message,true);}}
function showTab(id){activeTab=id;for(const t of document.querySelectorAll('.tab'))t.hidden=t.id!==id;for(const n of document.querySelectorAll('.nav'))n.classList.toggle('active',n.dataset.tab===id);$('#breadcrumb').textContent='Рабочее пространство / '+tabs[id];}
function empty(node,title,body){node.replaceChildren();const d=el('div',undefined,'empty');d.append(el('strong',title),el('span',body));node.append(d);}
function safeHH(url){try{const u=new URL(url);return u.protocol==='https:'&&(u.hostname==='hh.ru'||u.hostname.endsWith('.hh.ru'))?u.href:'#';}catch{return '#';}}
function hasDraftPlaceholders(text){
 return /\[(?:укажи|проверь|дополни|напиши)[^\]]*\]/i.test(text);
}
function renderTasks(){
 const root=$('#task-list');
 root.replaceChildren();
 if(!state.tasks.length)return empty(root,'Вопросов HR пока нет','Подтверждения получения резюме и отказы скрыты. Обнови чаты, чтобы проверить новые вопросы.');
 for(const t of state.tasks){
  const review=t.review;
  const card=el('article',undefined,'task');
  const head=el('div',undefined,'card-head');
  const heading=el('h2');
  const vacancy=el('a',review.vacancy_title||t.title);
  vacancy.href=safeHH(`https://hh.ru/vacancy/${review.vacancy_id}`);
  vacancy.target='_blank';vacancy.rel='noreferrer';
  heading.append(vacancy);
  head.append(heading,el('span',labels[t.status]||t.status,'chip'));
  card.append(head,el('p',review.employer||'Переписка по вакансии','sub'));
  const freshness=review.checked_at?'Проверено '+new Date(review.checked_at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'}):'Сохранённая переписка. Нажми «Проверить чаты» для обновления.';
  card.append(el('p',freshness,'reason'),el('h3','Вопрос HR'),el('div',t.body.replace(/\n{3,}/g,'\n\n'),'message'));
  card.append(el('p',review.source==='model'?'Черновик: '+review.model:'Запасной черновик по правилам. Подключи модель для ответа по контексту.','draft-source'));
  const context=el('details');
  context.append(el('summary','Контекст переписки'));
  for(const m of review.messages||[]){
   const bubble=el('div',undefined,'conversation-message '+(m.role==='APPLICANT'?'own':'hr'));
   bubble.append(el('strong',m.role==='APPLICANT'?'Ты':'HR'),el('p',m.text));
   for(const attachment of m.attachments||[])bubble.append(el('p','Вложение или ссылка: '+(attachment.title||'Файл'),'reason'));
   context.append(bubble);
  }
  card.append(context);
  if(review.missing?.length){
   const notes=el('div',undefined,'review-notes');
   notes.append(el('strong','Проверь перед отправкой'));
   for(const note of review.missing)notes.append(el('p',note));
   card.append(notes);
  }
  if(t.status==='open'){
   card.append(el('h3','Черновик ответа'));
   const key=t.id+':'+t.ref;
   const textarea=el('textarea');
   textarea.rows=8;
   textarea.placeholder='Проверь факты и дополни ответ.';
   textarea.value=taskDrafts.get(key)??t.draft??'';
   const previous=[...taskDrafts].find(([oldKey])=>typeof oldKey==='string'&&oldKey.startsWith(t.id+':')&&oldKey!==key);
   if(previous){
    const old=el('details');old.append(el('summary','HR дополнил переписку — предыдущий черновик'),el('p',previous[1],'letter'));card.append(old);
   }
   const actions=el('div',undefined,'task-actions');
   const send=el('button','Отправить HR','primary');
   send.dataset.requiresBrowser='true';send.dataset.taskId=t.id;send.dataset.ref=t.ref;
   const validate=()=>{send.dataset.invalid=String(!textarea.value.trim()||hasDraftPlaceholders(textarea.value)||review.can_send===false);send.disabled=state.running||send.dataset.invalid==='true';};
   textarea.addEventListener('input',()=>{taskDrafts.set(key,textarea.value);validate();});
   validate();
   send.addEventListener('click',()=>action(async()=>{
    if(!textarea.value.trim()||hasDraftPlaceholders(textarea.value))throw Error('Дополни отмеченные места в ответе.');
    send.dataset.pending='true';send.disabled=true;
    try{
     await api('/api/task',{id:t.id,text:textarea.value,expected_ref:t.ref});
     for(const oldKey of taskDrafts.keys())if(String(oldKey).startsWith(t.id+':'))taskDrafts.delete(oldKey);
     textarea.blur();toast('Ответ подтверждён в чате hh.ru');
    }finally{send.dataset.pending='false';validate();}
   }));
   const dismiss=el('button','Ответ не требуется','secondary');
   dismiss.addEventListener('click',()=>action(()=>api('/api/task',{id:t.id,dismiss:true})));
   actions.append(send,dismiss);
   const open=el('a','Открыть чат hh.ru ↗','secondary button-link');
   open.href=safeHH(`https://hh.ru/chat/${review.chat_id}`);open.target='_blank';open.rel='noreferrer';actions.append(open);
   card.append(textarea,actions,el('p','Ответ отправится после твоего нажатия. Места в квадратных скобках нужно заполнить.','reason'));
  }else{
   card.append(el('p','Проверь доставку в чате hh.ru. Повторная отправка заблокирована.','reason'));
   const open=el('a','Открыть чат hh.ru ↗','secondary button-link');open.href=safeHH(`https://hh.ru/chat/${review.chat_id}`);open.target='_blank';open.rel='noreferrer';card.append(open);
   if(t.status==='uncertain'){
    const dismiss=el('button','Проверил доставку, закрыть','secondary');dismiss.addEventListener('click',()=>action(()=>api('/api/task',{id:t.id,dismiss:true})));card.append(dismiss);
   }
  }
  root.append(card);
 }
}

function render(){
 const busy=Boolean(state.running||state.model_working);
 $('#cloud-browser-link').hidden=!state.cloud_browser;
 const count=state.tasks.length;
 $('#metric-found').textContent=state.counts?.suitable??state.vacancies.filter(v=>v.score>=60&&!['dismissed','filtered'].includes(v.status)).length;
 $('#metric-sent').textContent=state.counts?.applied??state.vacancies.filter(v=>v.status==='applied').length;
 $('#metric-tasks').textContent=count;$('#task-badge').textContent=count;
 $('#running-label').textContent=state.model_working?'Готовлю ответы с AI':state.running?(state.activity||'Выполняется операция'):state.settings.enabled?'Поиск включён':'Поиск на паузе';
 $('#running-dot').className=state.settings.enabled?'on':'';
 $('#toggle-run').textContent=state.settings.enabled?'Пауза':'Запустить';
 $('#resume-status').textContent=state.settings.resume_id?'Резюме выбрано':'Добавь ссылку на резюме в настройках';
 $('#criteria-summary').textContent=`Аналитика · Москва и удалёнка · без опыта или 1–3 года · от ${state.settings.min_salary.toLocaleString('ru-RU')} ₽ на руки`;
 $('#automation-detail').textContent=`Проверка каждые ${state.settings.interval_minutes} мин · ${state.settings.auto_apply?'отклики включены':'только поиск'}`;
 const model=state.model||{};
 const modelText=model.enabled&&model.key_saved?`Подключение сохранено: ${model.model}.`:'Модель не подключена. Пока доступны только запасные черновики по правилам.';
 $('#model-overview').textContent=modelText;$('#model-task-status').textContent=model.error||modelText;
 $('#model-status').textContent=model.error||modelText;
 $('#model-chip').textContent=model.enabled&&model.key_saved?'API настроен':'Не подключена';
 if(!loadedForm){
  for(const [key,value] of Object.entries(state.settings)){const input=$(`[name="${key}"]`);if(input)input.type==='checkbox'?input.checked=value:input.value=Array.isArray(value)?value.join('\n'):key==='resume_id'&&value?'https://hh.ru/resume/'+value:value;}
  $('#model-provider').value=model.provider||'deepseek';$('#model-name').value=model.model||'deepseek-flash';$('#model-enabled').checked=Boolean(model.enabled);
  $('#resume-context').value=state.resume_context||'';loadedForm=true;
 }
 $('#profile-status').textContent=state.resume_context?'Используется добавленный тобой текст резюме.':'Пока используется сохранённое резюме от 10 ноября. Сюда можно вставить обновлённый текст.';
 $('#api-status').textContent=state.api?.detail||'API hh.ru не подключён';
 $('#browser-connection-status').textContent=state.browser?.detail||(state.connected?'Вход подтверждён.':'Вход будет проверен перед откликом.');
 const banner=$('#dispatch-status');banner.hidden=!state.captcha;
 banner.textContent=state.captcha?'Поиск приостановлен для капчи. Пройди проверку в окне hh.ru.':'';
 $('#search').disabled=busy;
 $('#sync-chats').disabled=busy||chatSyncInFlight;
 $('#regenerate-drafts').disabled=busy||!model.enabled||!model.key_saved;
 $('#task-browser-status').textContent=busy?'Сейчас: '+(state.model_working?'подготовка ответов':state.activity||'обработка').concat('. Черновики можно читать; отправка станет доступна после завершения.'):'';
 if(!$('#task-list').contains(document.activeElement))renderTasks();
 for(const button of document.querySelectorAll('[data-requires-browser]')){
  const changed=button.dataset.taskId&&!state.tasks.some(t=>String(t.id)===button.dataset.taskId&&t.ref===button.dataset.ref);
  button.disabled=busy||button.dataset.pending==='true'||button.dataset.invalid==='true'||Boolean(changed);
 }
 const alerts=$('#important-alerts');alerts.replaceChildren();
 for(const item of (state.alerts||[]).filter(a=>a.status==='uncertain'||a.kind==='delivery').slice(0,3)){
  const card=el('article',undefined,'panel attention');card.append(el('h3',item.title),el('p',item.body));alerts.append(card);
 }
}
async function refresh(){const r=await fetch('/api/state');if(!r.ok)throw Error('Не удалось загрузить состояние');state=await r.json();csrf=state.csrf;render();}
for(const n of document.querySelectorAll('[data-tab]'))n.addEventListener('click',()=>showTab(n.dataset.tab));
for(const n of document.querySelectorAll('[data-goto]'))n.addEventListener('click',()=>showTab(n.dataset.goto));
$('#search').addEventListener('click',()=>action(async()=>{await api('/api/search',{dispatch:true});toast('Подбор и отклики запущены');}));
$('#toggle-run').addEventListener('click',()=>action(async()=>{await api('/api/settings',{enabled:!state.settings.enabled});}));
$('#settings-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{
 const data={};for(const input of e.target.elements){if(!input.name)continue;data[input.name]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.name==='queries'?input.value.split('\n').map(x=>x.trim()).filter(Boolean):input.value;}
 await api('/api/settings',data);await api('/api/resume-context',{text:$('#resume-context').value});loadedForm=false;toast('Условия и контекст резюме сохранены');
});});
$('#model-provider').addEventListener('change',()=>{$('#model-name').value=$('#model-provider').value==='router'?'deepseek-v4.1-flash-1017':$('#model-provider').value==='deepseek'?'deepseek-flash':'gpt-4.1-mini';$('#model-key').value='';});
$('#model-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{
 await api('/api/model',{provider:$('#model-provider').value,model:$('#model-name').value.trim(),api_key:$('#model-key').value.trim(),enabled:$('#model-enabled').checked});
 $('#model-key').value='';loadedForm=false;toast('Подключение сохранено. Нажми «Проверить API».');
});});
$('#test-model').addEventListener('click',()=>action(async()=>{await api('/api/model/test');toast('API отвечает. Подключение проверено.');}));
$('#regenerate-drafts').addEventListener('click',()=>action(async()=>{await api('/api/drafts');toast('Готовлю ответы по сохранённым перепискам. Свежие сообщения загружаются кнопкой «Обновить сообщения».');}));
$('#sync-chats').addEventListener('click',()=>action(async()=>{chatSyncInFlight=true;try{await api('/api/chats');toast('Сообщения обновлены');}finally{chatSyncInFlight=false;}}));
$('#login-browser').addEventListener('click',()=>action(async()=>{const d=await api('/api/login');toast(d.detail||'Открыт вход в hh.ru');}));
$('#check-session').addEventListener('click',()=>action(async()=>{const d=await api('/api/session');if(!d.logged_in)throw Error(d.detail||'Войди в hh.ru');toast('Вход подтверждён');}));
refresh().catch(e=>toast(e.message,true));setInterval(()=>refresh().catch(()=>{}),5000);
