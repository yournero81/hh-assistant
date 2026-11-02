const assert=require('node:assert/strict');
process.env.HH_ASSISTANT_HEADLESS='1';
process.env.HH_ASSISTANT_BROWSER_DIR=require('node:path').join(__dirname,'../data/chat-tests');
const {browser,run}=require('../browser.cjs');
const chats=require('../browser-chats.cjs');
const initial=()=>[{id:'1',own:true,text:'Мой отклик'},{id:'2',own:false,text:'Когда удобно интервью?'}];
let rows=initial(),draft='',disabled=false,unknown=false,logged=true,submits=0,persist=true,optimistic=true;
const escape=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
function bubble(m){return `<div data-qa="chatik-chat-message-${m.id}"><div class="${unknown?'unknown':'magritte-chat-bubble___fixture'} ${m.own?'magritte-outgoing___fixture':''}" data-qa="chatik-chat-message-bubble chatik-chat-message-${m.id}-text"><div data-qa="chat-bubble-message"><span data-qa="chat-bubble-text">${escape(m.text)}</span><span data-qa="chat-bubble-meta"><span data-qa="chat-bubble-display-time">16:48</span></span></div></div></div>`;}
(async()=>{
 const ctx=await browser();
 await ctx.exposeBinding('testSubmit',async(_,text)=>{submits++;const m={id:String(Number(rows.at(-1).id)+1),own:true,text};if(persist)rows.push(m);return optimistic?m:null;});
 await ctx.route('https://hh.ru/**',async route=>{
  const u=new URL(route.request().url());let body;
  if(!logged)body='<a href="/account/login">Войти</a>';
  else if(u.pathname==='/applicant/resumes')body='<a href="/applicant/profile/me">Профиль</a><a href="/resume/abcdef">Младший бизнес-аналитик</a>';
  else if(u.pathname==='/chat')body='<main id="chatik-layout"><h1>Чаты</h1><a data-qa="chatik-open-chat-20" href="/chat/20"><span data-qa="chat-cell-title">Аналитик</span><span data-qa="chat-cell-subtitle">Компания</span></a><a data-qa="chatik-skeleton-chat--1" href="/chat/-1"></a></main>';
  else body=`<main id="chatik-layout"><h1>Чаты</h1><div data-qa="chatik-header-sub-header">Вакансия Аналитик <a data-qa="chatik-header-vacancy-link" href="/vacancy/123">Перейти</a></div><div id="chatik_messages_scroller">${rows.map(bubble).join('')}</div><div data-qa="chatik-message-input"><textarea data-qa="text-input" ${disabled?'disabled':''}>${escape(draft)}</textarea></div></main><script>document.querySelector('textarea').addEventListener('keydown',async e=>{if(e.key==='Enter'){e.preventDefault();const m=await testSubmit(e.target.value);if(m){const wrap=document.createElement('div');wrap.setAttribute('data-qa','chatik-chat-message-'+m.id);const b=document.createElement('div');b.className='magritte-chat-bubble___fixture magritte-outgoing___fixture';b.setAttribute('data-qa','chatik-chat-message-bubble chatik-chat-message-'+m.id+'-text');const t=document.createElement('div');t.setAttribute('data-qa','chat-bubble-message');t.textContent=m.text;b.append(t);wrap.append(b);document.getElementById('chatik_messages_scroller').append(wrap);e.target.value='';}}});</script>`;
  await route.fulfill({status:200,contentType:'text/html; charset=utf-8',body});
 });
 logged=false;assert.equal((await chats.session(ctx)).status,'login_required');
 logged=true;const connection=await chats.session(ctx);assert.equal(connection.logged_in,true);assert.equal(connection.resumes[0].id,'abcdef');
 const list=await chats.list(ctx);assert.equal(list.items.length,1);assert.equal(list.items[0].id,'20');
 const history=await chats.read(ctx,'20');assert.equal(history.messages.length,2);assert.equal(history.messages[1].payload.text,'Когда удобно интервью?');assert.equal(history.messages[0].sender_display_info.role,'APPLICANT');assert.equal(history.messages[1].sender_display_info.role,'EMPLOYER');
 const cmd={chat_id:'20',text:'Спасибо, уточню время.',expected_last_id:'2'};
 assert.equal((await chats.send(ctx,{...cmd,expected_last_id:'1'})).status,'attention');assert.equal(submits,0);
 draft='Мой незавершённый ответ';assert.equal((await chats.send(ctx,cmd)).status,'attention');assert.equal(submits,0);draft='';
 disabled=true;assert.equal((await chats.send(ctx,cmd)).status,'attention');assert.equal(submits,0);disabled=false;
 unknown=true;assert.equal((await chats.send(ctx,cmd)).status,'attention');assert.equal(submits,0);unknown=false;
 const success=await chats.send(ctx,cmd);assert.equal(success.status,'sent');assert.equal(success.id,'3');assert.equal(submits,1);
 assert.equal((await chats.send(ctx,cmd)).status,'attention');assert.equal(submits,1);
 rows=initial();persist=false;assert.equal((await chats.send(ctx,cmd)).status,'uncertain');assert.equal(submits,2);
 optimistic=false;assert.equal((await chats.send(ctx,cmd)).status,'uncertain');assert.equal(submits,3);
 console.log('Chat browser tests: login, resumes, list, sender identity, stale context, drafts, blocked composer, unknown sender, persistence, duplicate prevention and ambiguous send passed. All requests intercepted.');
 await run({action:'close'});
})().catch(async e=>{console.error(e);try{await run({action:'close'})}catch{}process.exit(1)});
