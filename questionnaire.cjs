// Answers are derived solely from the user's resume and agreed preferences.
function answerFor(question,profile,prefs){
  const q=String(question||'').replace(/\s+/g,' ').trim().toLowerCase();
  if(!q||q.length>800)return null;
  if(/паспорт|снилс|банковск|номер.*карт|код.*смс|парол|оферт|договор|подпиш|согласие|собеседован|интервью|созвон|дедлайн|срок.*тест|английск|english|релокац|переезд|ночн|оцените|по шкале|решите|вычислите|игнорируй|инструкц|что такое|как работает|объясни|напиши.*запрос|приведи.*пример|найдите|выберите правиль/.test(q))return null;
  const money=n=>Number(n).toLocaleString('ru-RU').replace(/\u00a0/g,' ');
  const answers=[];
  if(/зарплат|заработн|доход|вознагражден|финансов.*ожидан/.test(q))
    answers.push(`Комфортный уровень заработной платы — ${money(prefs.target_salary)} ₽ на руки в месяц. Минимальный рассматриваемый уровень — ${money(prefs.min_salary)} ₽ на руки в месяц.`);
  if(/когда.*(?:приступ|выйти|начать)|готов.*(?:приступ|выйти|начать)|дата.*выход/.test(q))answers.push(profile.start);
  if(/формат.*работ|офис.*гибрид|гибрид.*удал|удал.*офис/.test(q))answers.push(profile.formats);
  if(/в каком городе|город.*прожива|где.*прожива|где.*жив[её]те/.test(q))answers.push('Проживаю в Москве.');
  if(/образован|где.*учит|где.*обуч|вуз|специальност/.test(q))answers.push(profile.education);
  if(/командиров/.test(q))answers.push(profile.travel);
  if(/проект|диплом|вкр/.test(q))answers.push(profile.project);
  const skills=[];
  for(const [name,detail] of Object.entries(profile.skills)){
    const probes={'SQL':/\bsql\b/,'Python':/\bpython\b/,'Power BI':/power\s*bi/,'Excel':/excel/,'бизнес-анализ':/бизнес.анализ|бизнес.процесс/};
    if(probes[name]?.test(q))skills.push(`${name}: ${detail}`);
  }
  if(skills.length)answers.push('Мои навыки: '+skills.join('; ')+'. Практический опыт связан с учебными и дипломным проектами.');
  if(/коммерческ|сколько.*лет.*опыт|стаж|опыт работы/.test(q)&&!skills.length&&!/проект|диплом|вкр/.test(q))return null;
  return answers.length?[...new Set(answers)].join('\n\n'):null;
}
async function fillQuestions(page,profile,prefs,rendered){
  const answered=[];const handledNames=[];
  for(const task of await page.locator('[data-qa="task-body"]').all()){
    if(!await rendered(task))continue;
    const question=(await task.locator('[data-qa="task-question"]').innerText()).trim();
    const answer=answerFor(question,profile,prefs);
    if(!answer)return {ok:false,question,answered,handledNames};
    const fields=task.locator('textarea,input:not([type="hidden"]),select');
    const actual=[];
    for(const field of await fields.all())if(await rendered(field))actual.push(field);
    if(actual.length!==1)return {ok:false,question,answered,handledNames};
    const field=actual[0];const type=await field.evaluate(e=>e.tagName==='TEXTAREA'?'textarea':e.type);
    if(!['textarea','text','search'].includes(type))return {ok:false,question,answered,handledNames};
    const current=await field.inputValue();
    if(current.trim()&&current!==answer)return {ok:false,question:'В поле уже есть другой ответ. '+question,answered,handledNames};
    await field.fill(answer);
    if(await field.inputValue()!==answer)return {ok:false,question,answered,handledNames};
    handledNames.push(await field.getAttribute('name'));
    answered.push({question,answer});
  }
  return {ok:true,answered,handledNames};
}
module.exports={answerFor,fillQuestions};
