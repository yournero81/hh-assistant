import base64,json,os,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import Mock,patch
import app

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=app.Store(Path(self.temp.name))
        self.vault=Mock();self.vault.read.return_value={}
        self.engine=app.Engine(self.store,self.vault)
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.make_handler(self.engine))
        self.engine.port=self.server.server_address[1]
        self.base='http://127.0.0.1:'+str(self.engine.port)
        self.env=patch.dict(os.environ,{'HH_ACCESS_PASSWORD':'private-test-password'});self.env.start()
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.env.stop();self.temp.cleanup()
    def request(self,path,payload=None,token=None,auth=True):
        headers={}
        if auth:headers['Authorization']='Basic '+base64.b64encode(b'timur:private-test-password').decode()
        if token:headers['X-CSRF-Token']=token
        data=json.dumps(payload).encode() if payload is not None else None
        return urllib.request.urlopen(urllib.request.Request(self.base+path,data=data,headers=headers),timeout=3)
    def test_private_state_rejects_unauthenticated_request(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:self.request('/api/state',auth=False)
        self.assertEqual(caught.exception.code,401);caught.exception.close()
        with self.request('/healthz',auth=False) as response:self.assertEqual(json.load(response),{'ok':True})
    def test_resume_context_is_saved_and_counts_are_database_wide(self):
        with self.request('/api/state') as response:state=json.load(response)
        with self.request('/api/resume-context',{'text':'Актуальный опыт'},state['csrf']):pass
        with self.request('/api/state') as response:state=json.load(response)
        self.assertEqual(state['resume_context'],'Актуальный опыт')
        self.assertIn('counts',state)
        self.assertNotIn('private-test-password',json.dumps(state))
    def test_mutation_without_csrf_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:self.request('/api/settings',{'enabled':True})
        self.assertEqual(caught.exception.code,403);caught.exception.close()

if __name__=='__main__':unittest.main()
