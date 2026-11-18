import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import app
from reply_model import ReplyModel,ModelError

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=app.Store(Path(self.temp.name))
        self.vault=Mock();self.vault.read.return_value={'model_keys':{'router':'test-key'}}
        self.model=ReplyModel(self.store,self.vault)
        self.store.save_settings({'model_provider':'router','model_name':'deepseek-v4.1-flash-1017','model_enabled':True})
        self.messages=[{'id':'2','payload':{'text':'Как понимаете JSON и XML?'},'sender_display_info':{'role':'EMPLOYER','is_current_participant':False}}]
    def tearDown(self):
        self.temp.cleanup()
    def test_key_is_not_in_status(self):
        self.assertNotIn('test-key',json.dumps(self.model.status()))
    def test_startup_does_not_make_paid_requests(self):
        self.model.request=Mock()
        self.assertEqual(self.model.review(self.messages,app.PROFILE,self.store.settings(),{}),(False,None))
        self.model.request.assert_not_called()
    def test_same_context_uses_cache_changed_resume_regenerates(self):
        self.model.request=Mock(return_value={'needs_reply':True,'draft':'JSON — формат данных.','missing':[],'reason':'Вопрос HR'})
        for _ in range(2):
            used,result=self.model.review(self.messages,app.PROFILE,self.store.settings(),{},refresh=True)
            self.assertTrue(used);self.assertEqual(result['source'],'model')
        self.assertEqual(self.model.request.call_count,1)
        self.model.review(self.messages,{'resume_text':'Новое резюме'},self.store.settings(),{},refresh=True)
        self.assertEqual(self.model.request.call_count,2)
    def test_informational_decision_hides_card(self):
        self.model.request=Mock(return_value={'needs_reply':False,'draft':'','missing':[],'reason':'Только информация'})
        self.assertEqual(self.model.review(self.messages,app.PROFILE,self.store.settings(),{},refresh=True),(True,None))
    def test_missing_or_invalid_response_is_not_a_draft(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"needs_reply":"yes","draft":"hi"}'}}]}).encode()
        with patch('reply_model.urllib.request.urlopen',return_value=response):
            with self.assertRaises(ModelError):
                self.model.request([],self.store.settings())
    def test_service_errors_do_not_leak_keys(self):
        with patch('reply_model.urllib.request.urlopen',side_effect=ValueError('test-key')):
            with self.assertRaisesRegex(ModelError,'корректный ответ') as caught:
                self.model.request([],self.store.settings())
            self.assertNotIn('test-key',str(caught.exception))

class GroundingTests(unittest.TestCase):
    def test_missing_system_analysis_years_cannot_turn_into_yes(self):
        from reply_model import ground_answer
        messages=[{'id':'2','payload':{'text':'1. Опыт работы от года в сфере системного анализа'},'sender_display_info':{'role':'EMPLOYER'}}]
        answer={'needs_reply':True,'draft':'1. Да, два года системным аналитиком.','missing':[],'reason':''}
        result=ground_answer(answer,app.PROFILE,app.DEFAULTS,messages)
        self.assertNotIn('1. Да',result['draft'])
        self.assertIn('Retention Analyst',result['draft'])
    def test_unknown_work_history_is_not_an_automatic_no(self):
        from reply_model import ground_answer
        messages=[{'id':'2','payload':{'text':'Есть опыт работы на производстве?'},'sender_display_info':{'role':'EMPLOYER'}}]
        answer={'needs_reply':True,'draft':'Опыта работы на производстве у меня нет.','missing':[],'reason':''}
        result=ground_answer(answer,app.PROFILE,app.DEFAULTS,messages)
        self.assertNotIn('у меня нет',result['draft'])
        self.assertIn('Retention Analyst',result['draft'])

class FilteringTests(unittest.TestCase):
    def test_rhetorical_heading_is_not_a_question(self):
        from hr_review import draft_reply
        messages=[{'id':'2','payload':{'text':'Спасибо за резюме!\nЧто дальше?\nМы изучим заявку и вернёмся с результатом.'},'sender_display_info':{'role':'EMPLOYER'}}]
        self.assertIsNone(draft_reply(messages,app.PROFILE,app.DEFAULTS))

if __name__=='__main__':unittest.main()
