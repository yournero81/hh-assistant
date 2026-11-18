import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from hr_review import draft_reply


def hr(text, mid='2', own=False):
    return {'id':mid,'payload':{'text':text},'sender_display_info':{
        'role':'APPLICANT' if own else 'EMPLOYER','is_current_participant':own}}


class DraftTests(unittest.TestCase):
    def test_receipts_and_verbose_rejections_are_filtered(self):
        for text in (
            'Спасибо за ваше резюме, скоро вернёмся.',
            'Рассмотрим ваше резюме. Если опыт подойдёт, мы свяжемся с вами.',
            'Благодарю за интерес к задачам. Мы не готовы пригласить вас на следующий этап собеседования. Успехов!',
        ):
            self.assertIsNone(draft_reply([hr(text)],app.PROFILE,app.DEFAULTS))

    def test_summaries_links_and_completed_answers_are_not_questions(self):
        for text in ('В разговоре с кандидатом я узнал: SQL? Ответ: да',
                     'Спасибо за отклик! https://example.com/?x=1',
                     'Готовы к офисному формату работы?: да'):
            self.assertIsNone(draft_reply([hr(text)],app.PROFILE,app.DEFAULTS))

    def test_unverified_industry_and_product_need_input(self):
        for text in ('Есть ли у вас опыт с ИСУП Адванта?',
                     'Есть у вас опыт работы системным аналитиком именно на производстве?'):
            result=draft_reply([hr(text)],app.PROFILE,app.DEFAULTS)
            self.assertTrue(result['missing'])
            self.assertNotIn('[Дополни',result['draft'])

    def test_multitopic_draft_uses_resume_and_marks_unknown_facts(self):
        result=draft_reply([hr('Какие ожидания по зарплате? Какой опыт работы и знания SQL? Какой уровень английского?')],app.PROFILE,app.DEFAULTS)
        self.assertIn('70 000',result['draft'])
        self.assertIn(app.PROFILE['experience'],result['draft'])
        self.assertIn('оконные функции',result['draft'])
        self.assertTrue(result['missing'])
        self.assertNotIn('[Дополни',result['draft'])
        self.assertNotIn('B2',result['draft'])

    def test_json_xml_get_content_without_invented_practice(self):
        result=draft_reply([hr('Ответьте: 1. Понимание структур JSON, XML?')],app.PROFILE,app.DEFAULTS)
        self.assertIn('JSON описывает',result['draft'])
        self.assertIn('XML представляет',result['draft'])
        self.assertNotIn('[Дополни',result['draft'])
        self.assertNotIn('работал с JSON',result['draft'])

    def test_interview_date_and_test_deadline_need_user_input(self):
        result=draft_reply([hr('Когда удобно пройти интервью? Когда сможете выполнить тестовое задание?')],app.PROFILE,app.DEFAULTS)
        self.assertIn('[укажи дату',result['draft'])
        self.assertIn('[Проверь',result['draft'])


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=app.Store(Path(self.tmp.name))
        vault=Mock();vault.read.return_value={}
        self.engine=app.Engine(self.store,vault)
        self.chat={'id':'20','vacancy_id':'123'}
        with self.store.connect() as db:
            db.execute('INSERT INTO vacancies VALUES (?,?,?,?,?,?,?)',('123',json.dumps({'name':'Аналитик','employer':{'name':'Компания'}}),90,'','letter','applied',app.now()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_card_per_chat_and_latest_context_replaces_old_question(self):
        self.engine.queue_hr_review(self.chat,[hr('Какие зарплатные ожидания?')],app.DEFAULTS)
        self.engine.queue_hr_review(self.chat,[hr('Какие зарплатные ожидания?'),hr('Когда готовы начать?',mid='3')],app.DEFAULTS)
        tasks=self.store.snapshot()['tasks']
        self.assertEqual(len(tasks),1)
        self.assertEqual(tasks[0]['ref'],'20:3')
        self.assertEqual(tasks[0]['review']['vacancy_title'],'Аналитик')
        self.assertEqual(tasks[0]['review']['employer'],'Компания')
        with self.assertRaisesRegex(ValueError,'новое сообщение'):
            self.engine.resolve_task(tasks[0]['id'],'Ответ',expected_ref='20:2')

    def test_answered_questions_and_later_rejection_close_card(self):
        self.engine.queue_hr_review(self.chat,[hr('Какие зарплатные ожидания?')],app.DEFAULTS)
        self.engine.queue_hr_review(self.chat,[hr('Какие зарплатные ожидания?'),hr('От 60 000',mid='3',own=True)],app.DEFAULTS)
        self.assertEqual(self.store.snapshot()['tasks'],[])
        self.engine.queue_hr_review(self.chat,[hr('Когда готовы начать?',mid='4')],app.DEFAULTS)
        self.engine.queue_hr_review(self.chat,[hr('Когда готовы начать?',mid='4'),hr('Мы не готовы пригласить вас на следующий этап собеседования.',mid='5')],app.DEFAULTS)
        self.assertEqual(self.store.snapshot()['tasks'],[])

    def test_unfilled_placeholder_cannot_be_sent(self):
        self.engine.queue_hr_review(self.chat,[hr('Когда удобно пройти интервью?')],app.DEFAULTS)
        task=self.store.snapshot()['tasks'][0]
        self.engine.send=Mock()
        with self.assertRaisesRegex(ValueError,'отмеченные места'):
            self.engine.resolve_task(task['id'],task['draft'],expected_ref=task['ref'])
        self.engine.send.assert_not_called()

    def test_successful_review_sends_only_approved_text(self):
        messages=[hr('Какие зарплатные ожидания?')]
        with self.store.connect() as db:
            db.execute('INSERT INTO messages VALUES (?,?,?,?)',('20','2',json.dumps(messages[0]),'seen'))
        self.engine.queue_hr_review(self.chat,messages,app.DEFAULTS)
        task=self.store.snapshot()['tasks'][0]
        self.engine.send=Mock(return_value={'status':'sent'})
        self.engine.resolve_task(task['id'],'Мой проверенный ответ',expected_ref=task['ref'])
        self.engine.send.assert_called_once_with('20','Мой проверенный ответ',automated=False)
        self.assertEqual(self.store.snapshot()['tasks'],[])


if __name__=='__main__':
    unittest.main()
