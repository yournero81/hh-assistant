// Only the ordinary hh.ru UI. No private endpoints, cookies export or API tokens.
const CHAT_CELL='a[data-qa^="chatik-open-chat-"]';
const MESSAGE='[data-qa^="chatik-chat-message-"]';
const norm=s=>String(s||'').replace(/\s+/g,' ').trim();
class ChatError extends Error {
  constructor(message,status='attention'){super(message);this.status=status;}
}
async function open(page,url){
  await page.goto(url,{waitUntil:'domcontentloaded'});
  const body=await page.locator('body').innerText();
  if(/captcha|капч|подтвердите.*не робот|Доступ ограничен/i.test(body))throw new ChatError('Пройди проверку hh.ru в открытом браузере.');
  if(/\/account\/login|\/login/.test(page.url())||await page.getByRole('link',{name:'Войти',exact:true}).isVisible().catch(()=>false))
    throw new ChatError('Войди в hh.ru в отдельном окне программы, затем нажми «Проверить вход».','login_required');
}
async function scope(page){
  // The full-screen /chat route renders the UI directly. Some layouts embed it.
  if(await page.locator('#chatik-layout').count())return page;
  const embedded=page.frames().find(f=>/^https:\/\/chatik\.hh\.ru\//.test(f.url()));
  return embedded||page;
}
async function readMessages(root){
  return root.locator(MESSAGE).evaluateAll(es=>es.filter(e=>/^chatik-chat-message-\d+$/.test(e.getAttribute('data-qa')||'')).map(e=>{
    const id=e.getAttribute('data-qa').slice('chatik-chat-message-'.length);
    const bubble=e.querySelector('[data-qa~="chatik-chat-message-bubble"]');
    const content=e.querySelector('[data-qa="chat-bubble-message"]');
    const textNode=e.querySelector('[data-qa="chat-bubble-text"]');
    const title=e.querySelector('[data-qa="chat-bubble-message-title"]')?.textContent.trim()||'';
    const copy=content?.cloneNode(true);
    copy?.querySelectorAll('[data-qa="chat-bubble-meta"],[data-qa="chat-bubble-message-title"]').forEach(n=>n.remove());
    const text=textNode?.innerText.trim()||copy?.textContent.trim()||title;
    const own=bubble&&/(?:^|\s)magritte-outgoing___/.test(bubble.className);
    const recognised=!!bubble&&/(?:^|\s)magritte-chat-bubble___/.test(bubble.className);
    const system=!!e.querySelector('[data-qa^="participant-action-message-"]');
    const attachments=[...e.querySelectorAll('a[href]')].filter(a=>a.hasAttribute('download')||!/^https:\/\/hh\.ru\/(?:chat|employer|resume)\//.test(a.href)).map(a=>({title:a.textContent.trim()||'Вложение или ссылка'}));
    return {id,creation_time:'',type:system?'PARTICIPANT_EVENT':'SIMPLE',payload:{text,attachments},status_title:title,
      sender_display_info:{role:system?'SYSTEM':recognised?(own?'APPLICANT':'EMPLOYER'):'UNKNOWN',is_current_participant:recognised?!!own:null},
      _unrecognised:!recognised&&!system,_parser_version:2};
  }));
}
async function header(root){
  const links=await root.locator('[data-qa="chatik-header-vacancy-link"]').evaluateAll(es=>es.map(e=>e.href));
  return {vacancy_id:links[0]?.match(/\/vacancy\/(\d+)/)?.[1]||null,
    title:(await root.locator('[data-qa="chatik-header-sub-header"]').allTextContents()).join(' ').trim()};
}
async function history(page,id){
  if(!/^\d+$/.test(String(id)))throw new ChatError('Некорректный идентификатор чата');
  await open(page,`https://hh.ru/chat/${id}`);
  const root=await scope(page);
  await root.locator('#chatik_messages_scroller,[data-qa="chatik_messages_scroller"]').waitFor({timeout:15000});
  // Numeric message wrappers are the source of identity and ordering, never text hashes.
  try{await root.locator(MESSAGE).first().waitFor({timeout:8000});}catch{}
  const messages=await readMessages(root);
  if(!messages.length){
    const text=await root.locator('#chatik_messages_scroller,[data-qa="chatik_messages_scroller"]').innerText();
    if(text.trim()&&!/нет сообщений|Начните общаться/i.test(text))throw new ChatError('Сообщения отображаются, но их структура не распознана. Проверь чат вручную.');
  }
  const input=root.locator('[data-qa="chatik-message-input"] textarea[data-qa="text-input"]');
  const allowed=await input.count()===1&&await input.isVisible()&&await input.isEnabled();
  return {root,input,messages,has_more:false,complete_history:false,
    chat_states:{write_message_state:{allowed}},...await header(root)};
}
async function session(ctx){
  const page=await ctx.newPage();
  try{
    await open(page,'https://hh.ru/applicant/resumes');
    const logged=await page.locator('a[href*="/applicant/profile/me"],[data-qa="mainmenu_applicantProfile"],[data-qa="mainmenu_resumes"]').count()>0;
    if(!logged)throw new ChatError('Войди в hh.ru в этом окне, затем проверь вход ещё раз.','login_required');
    await page.locator('a[href*="/resume/"]').first().waitFor({state:'visible',timeout:10000}).catch(()=>{});
    const resumes=await page.locator('a[href*="/resume/"]').evaluateAll(es=>es.map(e=>({
      id:e.href.match(/\/resume\/([a-zA-Z0-9]+)/)?.[1],title:e.textContent.trim(),url:e.href
    })).filter(e=>e.id&&e.title));
    const unique=[...new Map(resumes.map(r=>[r.id,r])).values()];
    await page.close();return {status:'connected',logged_in:true,resumes:unique,detail:'Вход в браузере подтверждён. API и регистрация приложения не нужны.'};
  }catch(e){return {status:e.status||'attention',logged_in:false,resumes:[],detail:e.message};}
}
async function list(ctx){
  const page=await ctx.newPage();
  try{
    await open(page,'https://hh.ru/chat');
    const root=await scope(page);
    await root.getByText('Чаты',{exact:true}).first().waitFor({timeout:15000});
    try{await root.locator(CHAT_CELL).first().waitFor({timeout:10000});}catch{
      const text=await root.locator('body').innerText();
      if(/Начните общаться|Пока нет чатов|нет сообщений/i.test(text)){await page.close();return {items:[],pages:1};}
      throw new ChatError('Список чатов не распознан. Проверь открытую вкладку.');
    }
    const seen=new Map();let stable=0;let previous='';let truncated=true;
    for(let step=0;step<30;step++){
      const cells=await root.locator(CHAT_CELL).evaluateAll(es=>es.map(e=>({
        id:e.getAttribute('data-qa').match(/^chatik-open-chat-(\d+)$/)?.[1],
        display:{title:[...e.querySelectorAll('[data-qa="chat-cell-title"],[data-qa="chat-cell-subtitle"]')].map(x=>x.textContent.trim()).join(' · ')},
        type:'NEGOTIATION',block_reason:null
      })).filter(e=>e.id));
      for(const cell of cells)seen.set(cell.id,cell);
      const signature=cells.map(c=>c.id).join(',');
      stable=signature===previous?stable+1:0;previous=signature;
      const loading=root.locator('[data-qa="chats-list-skeleton-wrapper"]');
      if(stable>=2){truncated=await loading.count()>0&&await loading.isVisible();break;}
      if(seen.size>=200)break;
      const last=root.locator(CHAT_CELL).last();await last.scrollIntoViewIfNeeded();
      // UI-driven scroll triggers lazy rendering; wait for a changed row or the
      // disappearance of the loading skeleton, with a bounded time budget.
      const skeleton=root.locator('[data-qa="chats-list-skeleton-wrapper"]');
      if(await skeleton.count())await skeleton.waitFor({state:'hidden',timeout:1500}).catch(()=>{});
    }
    await page.close();return {items:[...seen.values()].slice(0,200),pages:1,truncated};
  }catch(e){return {status:e.status||'attention',detail:e.message};}
}
async function read(ctx,id){
  const page=await ctx.newPage();
  try{
    const result=await history(page,id);
    const {root,input,...serializable}=result;
    await page.close();return serializable;
  }catch(e){return {status:e.status||'attention',detail:e.message};}
}
async function send(ctx,cmd){
  const page=await ctx.newPage();let clicked=false;
  try{
    if(typeof cmd.text!=='string'||!cmd.text.trim()||cmd.text.length>20000)throw new ChatError('Пустое или слишком длинное сообщение');
    const before=await history(page,cmd.chat_id);
    const sorted=[...before.messages].sort((a,b)=>BigInt(a.id)<BigInt(b.id)?-1:1);
    const last=sorted.at(-1);
    if(!last||!cmd.expected_last_id||last.id!==String(cmd.expected_last_id))throw new ChatError('Появились новые сообщения или контекст не проверен. Обнови переписку перед ответом.');
    if(before.messages.some(m=>m._unrecognised))throw new ChatError('Не удалось определить отправителя одного из сообщений. Требуется проверка вручную.');
    if(!before.chat_states.write_message_state.allowed)throw new ChatError('В этом чате отправка недоступна.');
    if((await before.input.inputValue()).trim())throw new ChatError('В чате уже есть черновик. Он не был изменён.');
    await before.input.fill(cmd.text);
    if(await before.input.inputValue()!==cmd.text)throw new ChatError('Не удалось проверить текст ответа');
    const button=before.root.locator('button[data-qa="chatik-message-send"]');
    // A single Enter is the normal, observed chat composer action. Multi-line
    // content is filled as text first; never inject keyboard shortcuts from HR.
    clicked=true;
    if(await button.count()===1&&await button.isVisible())await button.click();
    else await before.input.press('Enter');
    const ids=new Set(before.messages.map(m=>m.id));
    try{
      await page.waitForFunction(({text,ids})=>[...document.querySelectorAll('[data-qa^="chatik-chat-message-"]')].some(e=>{
        const qa=e.getAttribute('data-qa')||'';if(!/^chatik-chat-message-\d+$/.test(qa)||ids.includes(qa.slice('chatik-chat-message-'.length)))return false;
        const bubble=e.querySelector('[data-qa~="chatik-chat-message-bubble"]');
        const content=e.querySelector('[data-qa="chat-bubble-message"]');
        const textNode=e.querySelector('[data-qa="chat-bubble-text"]');
        const copy=content?.cloneNode(true);copy?.querySelectorAll('[data-qa="chat-bubble-meta"],[data-qa="chat-bubble-message-title"]').forEach(n=>n.remove());
        const body=textNode?.innerText||copy?.textContent||'';
        return bubble?.className.includes('magritte-outgoing___')&&body.replace(/\s+/g,' ').trim()===text;
      }),{text:norm(cmd.text),ids:[...ids]},{timeout:12000});
    }catch{throw new ChatError('Нет подтверждения отправки. Проверь чат; повтор заблокирован.','uncertain');}
    // Require persistence after reloading the ordinary UI, not just an optimistic bubble.
    const after=await history(page,cmd.chat_id);
    const confirmed=after.messages.find(m=>!ids.has(m.id)&&m.sender_display_info.is_current_participant===true&&norm(m.payload.text)===norm(cmd.text));
    if(!confirmed)throw new ChatError('Сообщение не подтверждено после перезагрузки чата. Проверь вручную.','uncertain');
    await page.close();return {status:'sent',id:confirmed.id,message:confirmed};
  }catch(e){return {status:clicked?'uncertain':e.status||'attention',detail:e.message};}
}
module.exports={session,list,read,send,readMessages,norm};
