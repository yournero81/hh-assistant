const {chromium} = require('playwright');
const readline = require('node:readline');
const path = require('node:path');
const chats = require('./browser-chats.cjs');
const questionnaire=require('./questionnaire.cjs');
const candidateProfile=require(require('node:fs').existsSync(path.join(__dirname,'profile.json'))?'./profile.json':'./profile.example.json');
let context;
async function browser() {
  if (!context) {
    context = await chromium.launchPersistentContext(process.env.HH_ASSISTANT_BROWSER_DIR||path.join(__dirname,'data','browser'), {headless:process.env.HH_ASSISTANT_HEADLESS==='1', viewport:{width:1200,height:850}});
    context.setDefaultTimeout(12000);
    context.on('close',()=>context=null);
  }
  return context;
}
async function visible(page, selector) {
  const items=page.locator(selector);
  for(let i=0;i<await items.count();i++) if(await items.nth(i).isVisible()) return items.nth(i);
  return null;
}
// isVisible()/innerText include content clipped by a collapsed ancestor.
// hh.ru keeps inactive visibility warnings in such zero-height containers.
async function rendered(locator){
  return locator.evaluateAll(elements=>elements.some(e=>{
    let r=e.getBoundingClientRect();let left=r.left,right=r.right,top=r.top,bottom=r.bottom;
    if(right-left<1||bottom-top<1)return false;
    for(let p=e;p;p=p.parentElement){
      const style=getComputedStyle(p);
      if(style.display==='none'||style.visibility==='hidden'||Number(style.opacity)===0)return false;
      if(p!==e){
        const box=p.getBoundingClientRect();
        if(/hidden|clip|auto|scroll/.test(style.overflowX)){left=Math.max(left,box.left);right=Math.min(right,box.right);}
        if(/hidden|clip|auto|scroll/.test(style.overflowY)){top=Math.max(top,box.top);bottom=Math.min(bottom,box.bottom);}
        if(right-left<1||bottom-top<1)return false;
      }
    }
    return true;
  }));
}
async function shown(page,pattern){return rendered(page.getByText(pattern));}
async function applicationReceipt(page,id){
  const success=/Отклик\s+отправлен|Вы\s+откликнулись|Вы\s+успешно\s+откликнулись/i;
  let confirmed=false;
  for(let attempt=0;attempt<60;attempt++){
    if(page.isClosed())return null;
    try{if(await shown(page,success)){confirmed=true;break;}}catch{}
    await page.waitForTimeout(250).catch(()=>{});
  }
  if(!confirmed){
    // A read-only check can recover a successful POST followed by navigation.
    // This never presses the submit button a second time.
    await page.goto(`https://hh.ru/applicant/vacancy_response?vacancyId=${id}`,{waitUntil:'domcontentloaded'});
    for(let attempt=0;attempt<40;attempt++){
      try{if(await shown(page,success)){confirmed=true;break;}}catch{}
      await page.waitForTimeout(250);
    }
  }
  if(!confirmed)return null;
  let chatId=page.url().match(/\/chat\/(\d+)/)?.[1];
  const chat=page.getByRole('button',{name:'Чат',exact:true});
  if(!chatId&&await chat.count()===1&&await rendered(chat)){
    await chat.click();
    const frame=page.locator('iframe[src*="chatik.hh.ru/chat/"]');
    await frame.first().waitFor({state:'attached',timeout:5000}).catch(()=>{});
    if(await frame.count())chatId=(await frame.first().getAttribute('src')).match(/\/chat\/(\d+)/)?.[1];
  }
  return {chat_id:chatId};
}
function salaryFrom(text) {
  const line=text.split('\n').find(s=>/₽|руб\./.test(s));
  if(!line) return null;
  const nums=[...line.matchAll(/\d[\d\s\u00a0\u202f]*\d|\d/g)].map(m=>Number(m[0].replace(/\s/g,''))).filter(n=>n>=1000);
  if(!nums.length) return null;
  let from=null,to=null;
  if(nums.length>=2) [from,to]=nums;
  else if(/до\s/i.test(line)) to=nums[0]; else from=nums[0];
  return {from,to,currency:'RUR',gross:/до\s+вычета|до\s+налогов/i.test(line)?true:/на\s+руки/i.test(line)?false:null,
    _period_uncertain:/за\s+(?:смену|час|год|неделю|день)/i.test(line)};
}
function experienceFrom(text) {
  if(/более 6|свыше 6/i.test(text)) return {id:'moreThan6',name:'Более 6 лет'};
  if(/3\s*[–—-]\s*6/.test(text)) return {id:'between3And6',name:'3–6 лет'};
  if(/1\s*[–—-]\s*3/.test(text)) return {id:'between1And3',name:'1–3 года'};
  if(/без опыта|не требуется/i.test(text)) return {id:'noExperience',name:'Без опыта'};
  return {id:'unknown',name:'Опыт не определён — требуется проверка'};
}
async function readable(page,url) {
  await page.goto(url,{waitUntil:'domcontentloaded'});
  const text=await page.locator('body').innerText();
  if(/captcha|капч|подтвердите.*не робот|Доступ ограничен/i.test(text)) throw Error('В браузере нужна проверка hh.ru. Пройди её вручную и повтори поиск.');
  return text;
}
async function run(cmd) {
  if(cmd.action==='close') { if(context) await context.close(); return {status:'closed'}; }
  const ctx=await browser();
  if(cmd.action==='session')return chats.session(ctx);
  if(cmd.action==='chats')return chats.list(ctx);
  if(cmd.action==='chat')return chats.read(ctx,cmd.chat_id);
  if(cmd.action==='send_message')return chats.send(ctx,cmd);
  if(cmd.action==='open_chats'){
    const page=await ctx.newPage();
    await page.goto('https://hh.ru/chat',{waitUntil:'domcontentloaded'});
    return {status:'opened'};
  }
  if(cmd.action==='search') {
    const p=await ctx.newPage();
    try {
      const q=new URLSearchParams({text:cmd.params.text,area:cmd.params.area,search_field:'name',order_by:'publication_time',date_from:new Date(Date.now()-7*86400000).toISOString().slice(0,10),page:String(cmd.params.page||0)});
      if(cmd.params.work_format)q.set('work_format',cmd.params.work_format);
      q.set('experience','noExperience');
      await readable(p,'https://hh.ru/search/vacancy?'+q);
      try { await p.locator('[data-qa="serp-item__title"],[data-qa="vacancy-serp__vacancy-title"]').first().waitFor({timeout:15000}); }
      catch {
        if(/Ничего не найдено|не найдено ни одной|По вашему запросу ничего/i.test(await p.locator('body').innerText())) {await p.close();return {items:[],pages:1};}
        throw Error('Карточки поиска не найдены. Проверь открывшуюся страницу hh.ru.');
      }
      const cards=await p.locator('[data-qa="serp-item__title"],[data-qa="vacancy-serp__vacancy-title"]').evaluateAll(links=>links.map(a=>{
        const card=a.closest('[data-qa="vacancy-serp__vacancy"],.serp-item')||a.parentElement.parentElement.parentElement;
        const employer=card.querySelector('[data-qa="vacancy-serp__vacancy-employer"]');
        const address=card.querySelector('[data-qa="vacancy-serp__vacancy-address"]');
        return {name:a.textContent.trim(),url:a.href,text:card.innerText,employer:employer?.textContent.trim()||'',area:address?.textContent.trim()||''};
      }));
      const items=[];
      for(const c of cards){const id=c.url.match(/\/vacancy\/(\d+)/)?.[1];if(!id)continue;const remote=/Можно удал[её]нно|Формат работы:.*удал[её]нно/i.test(c.text);items.push({id,name:c.name,alternate_url:`https://hh.ru/vacancy/${id}`,employer:{name:c.employer},area:{id:/Москва/.test(c.area)?'1':cmd.params.area==='1'&&!c.area?'1':'unknown',name:c.area||'Регион уточняется'},salary:salaryFrom(c.text),experience:experienceFrom(c.text),work_format:remote?[{id:'REMOTE'}]:[],snippet:{requirement:c.text},_source:'browser'});}
      const hasNext=await p.locator('[data-qa="pager-next"]').count()>0;
      await p.close();return {items,pages:hasNext?Number(cmd.params.page||0)+2:Number(cmd.params.page||0)+1};
    }catch(e){return {status:'error',detail:e.message.split('\n')[0]};}
  }
  if(cmd.action==='detail') {
    if(!/^\d+$/.test(cmd.id))throw Error('Некорректный ID');
    const p=await ctx.newPage();
    try {
      const text=await readable(p,`https://hh.ru/vacancy/${cmd.id}`);
      const desc=p.locator('[data-qa="vacancy-description"]');await desc.waitFor();
      const name=await p.locator('[data-qa="vacancy-title"]').innerText();
      const description=await desc.innerText();
      const exp=await p.locator('[data-qa="vacancy-experience"]').allTextContents();
      const salary=await p.locator('[data-qa="vacancy-salary"],[data-qa="vacancy-salary-compensation-type-net"],[data-qa="vacancy-salary-compensation-type-gross"]').allTextContents();
      const employer=await p.locator('[data-qa="vacancy-company-name"]').allTextContents();
      const remote=/Формат работы:[\s\S]{0,100}(?:удал[её]нно)|Можно удал[её]нно/i.test(text);
      const area=await p.locator('[data-qa="vacancy-view-location"],[data-qa="vacancy-view-raw-address"]').allTextContents();
      const result={...(cmd.brief||{}),id:cmd.id,name,description,alternate_url:`https://hh.ru/vacancy/${cmd.id}`,employer:{name:employer.join(' ').trim()||(cmd.brief?.employer?.name||'')},salary:salaryFrom(salary.join('\n')||text.split(/Требуемый опыт|Опыт работы/)[0]),experience:experienceFrom(exp.join(' ')||text),work_format:remote?[{id:'REMOTE'}]:[],_source:'browser'};
      if(area.length)result.area={id:/Москва/.test(area.join(' '))?'1':'unknown',name:area.join(' ')};
      if(/Вакансия в архиве/i.test(text))result.archived=true;
      await p.close();return result;
    }catch(e){return {status:'error',detail:e.message.split('\n')[0]};}
  }
  if(cmd.action==='login') {
    const p=await ctx.newPage(); await p.goto('https://hh.ru/account/login?backurl=%2Fapplicant%2Fresumes', {waitUntil:'domcontentloaded'});
    return {status:'opened',detail:'Войди в hh.ru и скопируй ссылку на нужное резюме в настройки.'};
  }
  if(cmd.action!=='apply'||!/^\d+$/.test(cmd.id)||! /^[a-zA-Z0-9]+$/.test(cmd.resume_id)) throw Error('Некорректная команда или ID резюме');
  const p=await ctx.newPage(); let submitted=false,answered=[];
  try {
    await p.goto(`https://hh.ru/applicant/vacancy_response?vacancyId=${cmd.id}`, {waitUntil:'domcontentloaded'});
    await p.locator('body').waitFor();
    if(new URL(p.url()).hostname!=='hh.ru') return {status:'attention',pause_required:false,detail:'Внешняя форма: требуется ручной отклик.'};
    const body=await p.locator('body').innerText();
    if(/captcha|капч|подтвердите.*не робот|войдите.*аккаунт|Вход в личный кабинет/i.test(body)||/account\/login/.test(p.url())) return {status:'attention',detail:'Требуется вход или проверка в браузере.'};
    if(await shown(p,/Вы\s+уже\s+откликнулись|Вы\s+откликнулись|Отклик\s+отправлен/i)) return {status:'already',detail:'hh.ru сообщает, что отклик уже существует.'};
    // Do not guess which resume a radio label represents: require its actual ID.
    if(!await p.locator(`input[type="radio"][value="${cmd.resume_id}"]`).count()){
      const choosers=p.locator('[role="button"]').filter({has:p.locator('[data-qa="resume-title"]')});
      for(let attempt=0;attempt<60;attempt++){
        if(await shown(p,/Вы\s+(?:уже\s+)?откликнулись|Отклик\s+отправлен/i))return {status:'already',detail:'hh.ru сообщает, что отклик уже существует.'};
        let chooser;
        for(const c of await choosers.all())if(await rendered(c)){chooser=c;break;}
        if(chooser){await chooser.click();break;}
        await p.waitForTimeout(250);
      }
      await p.locator(`input[type="radio"][value="${cmd.resume_id}"]`).first().waitFor({state:'attached',timeout:8000}).catch(()=>{});
    }
    const radio=p.locator(`input[type="radio"][value="${cmd.resume_id}"]`);
    if(await radio.count()===1) await radio.check({force:true});
    else {
      const selected=p.locator(`input[name="resume_id"][value="${cmd.resume_id}"],input[name="resume_hash"][value="${cmd.resume_id}"]`);
      const selectedLink=p.locator(`a[href*="/resume/${cmd.resume_id}"]`);
      const choices=p.locator('input[type="radio"]');
      if(!await selected.count() && !(await selectedLink.count()===1 && await choices.count()<=1))
        return {status:'attention',detail:'Не удалось однозначно подтвердить выбранное резюме. Проверь форму в открытой вкладке.',debug:{url:p.url(),resume_titles:await p.locator('[data-qa="resume-title"]').allTextContents(),radio_values:await p.locator('input[type="radio"]').evaluateAll(es=>es.map(e=>e.value))}};
    }
    if(await shown(p,/поменяйте\s+видимость|измените\s+видимость/i))return {status:'attention',pause_required:false,detail:'Для выбранного резюме действительно показано требование изменить видимость. Приватность не изменена; проверь форму самостоятельно.'};
    const questions=await questionnaire.fillQuestions(p,candidateProfile,cmd.preferences||{min_salary:60000,target_salary:70000},rendered);
    answered=questions.answered;
    if(!questions.ok)return {status:'attention',pause_required:false,question:questions.question,detail:'Нужен твой ответ в анкете: '+questions.question};
    if(!questions.answered.length&&await shown(p,/пройти\s+тест|тестовое\s+задание/i))return {status:'attention',pause_required:false,detail:'Перед откликом требуется тест или анкета. Заполни самостоятельно в браузере.'};
    const fields=p.locator('#RESPONSE_MODAL_FORM_ID input:not([type="hidden"]),#RESPONSE_MODAL_FORM_ID textarea,#RESPONSE_MODAL_FORM_ID select');
    for(const field of await fields.all()){
      const meta=await field.evaluate(e=>({qa:e.getAttribute('data-qa')||'',name:e.getAttribute('name')||'',type:e.type,value:e.value}));
      if(/letter/.test(meta.qa+' '+meta.name)||meta.value===cmd.resume_id||/resume/.test(meta.name)||questions.handledNames.includes(meta.name))continue;
      if(await rendered(field))return {status:'attention',pause_required:false,detail:'В форме есть вопросы работодателя. Отклик оставлен на ручную проверку.'};
    }
    let letter=await visible(p,'textarea[data-qa="vacancy-response-popup-form-letter-input"],textarea[name="letter"],textarea[data-qa="vacancy-response-letter-toggle-textarea"]');
    if(!letter) {
      let toggle=p.getByRole('button',{name:/Сопроводительное\s+письмо.*Добавить/i}).first();
      if(!await toggle.count())toggle=p.getByText(/Добавить\s+сопроводительное/i).first();
      if(await toggle.isVisible()) await toggle.click();
      await p.locator('textarea[data-qa="vacancy-response-popup-form-letter-input"],textarea[name="letter"],textarea[data-qa="vacancy-response-letter-toggle-textarea"]').first().waitFor({state:'visible',timeout:5000}).catch(()=>{});
      letter=await visible(p,'textarea[data-qa="vacancy-response-popup-form-letter-input"],textarea[name="letter"],textarea[data-qa="vacancy-response-letter-toggle-textarea"]');
    }
    if(!letter) return {status:'attention',detail:'Поле сопроводительного не найдено. Форма оставлена открытой.'};
    await letter.fill(cmd.letter);
    if(await letter.inputValue()!==cmd.letter) throw Error('Письмо не заполнилось');
    const submit=await visible(p,'button[data-qa="vacancy-response-submit-popup"],button[data-qa="vacancy-response-submit"],button[data-qa="vacancy-response-button"]');
    if(!submit||!await submit.isEnabled()) return {status:'attention',detail:'Кнопка отправки недоступна; проверь форму.'};
    // After clicking, an unconfirmed outcome must NEVER be retried automatically.
    submitted=true;
    await submit.click();
    const receipt=await applicationReceipt(p,cmd.id);
    if(!receipt)return {status:'uncertain',answers:answered,detail:'Кнопка нажата, но подтверждение не найдено. Проверь отклики hh.ru; повторная отправка заблокирована.'};
    await p.close(); return {status:'applied',chat_id:receipt.chat_id,answers:answered,detail:'Получено подтверждение hh.ru.'};
  } catch(e) {
    if(submitted){
      try{
        const receipt=await applicationReceipt(p,cmd.id);
        if(receipt){await p.close();return {status:'applied',chat_id:receipt.chat_id,answers:answered,detail:'Отправка подтверждена повторной проверкой страницы hh.ru.'};}
      }catch{}
    }
    return {status:submitted?'uncertain':'attention',answers:answered,detail:(submitted?'Результат отправки неизвестен. Проверь hh.ru вручную. ':'Браузер: ')+e.message.split('\n')[0]};
  }
}
module.exports={run,browser,salaryFrom,experienceFrom,rendered};
if(require.main===module){
  let chain=Promise.resolve();
  readline.createInterface({input:process.stdin}).on('line',line=> {
    chain=chain.then(async()=>{try {const c=JSON.parse(line); process.stdout.write(JSON.stringify(await run(c))+'\n');}catch(e){process.stdout.write(JSON.stringify({status:'error',detail:e.message})+'\n');}});
  });
  process.stdin.on('end',()=>{chain.finally(async()=>{if(context) await context.close();});});
}
