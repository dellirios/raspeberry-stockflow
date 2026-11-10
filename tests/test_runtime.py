import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from stockflow.runtime import create_server, polling_delay, InstanceLock, run_agent
from stockflow.service import Agent
from test_feedback import Printer, Clock, snapshot
from stockflow.config import load_env, Settings
from stockflow.service import quantity


class FakeAgent:
    def __init__(self):
        self.settings = type('Settings',(),{'poll_url':'https://stockflow.pro/start.php','token':''})()
        self.called = []
    def health(self): return {'status':'ok','uptime':1,'stats':{'processed':2,'failures':0,'duplicates':0}}
    def request(self,*args,**kwargs): self.called.append(args); return {'data':[]}
    def process(self,data): return []


class RuntimeTests(unittest.TestCase):
    def test_loopback_http_health_metrics_and_no_external_fetch(self):
        agent = FakeAgent()
        server = create_server(agent,'127.0.0.1',0)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            client = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            client.request('GET','/health')
            response = client.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(json.loads(response.read())['status'],'ok')
            client.request('GET','/metrics')
            response = client.getresponse()
            self.assertIn(b'stockflow_processed_total 2',response.read())
            client.request('GET','/imprimir-etiqueta?url=http%3A%2F%2Fevil.test%2F')
            response = client.getresponse()
            self.assertEqual(response.status,400)
            response.read()
            self.assertFalse(agent.called)
            client.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_one_second_polling_has_jitter_and_bounded_backoff(self):
        settings = Settings('.',{'EMPTY_POLL_BASE_DELAY_MS':'1000','NOOP_PAYLOAD_BASE_DELAY_MS':'1000','MAX_IDLE_BACKOFF_MS':'1000'})
        self.assertEqual(polling_delay(settings,8,False,random_value=0),.7)
        self.assertAlmostEqual(polling_delay(settings,8,True,random_value=1),1.3)

    def test_second_process_cannot_take_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'agent.lock'
            with InstanceLock(path):
                with self.assertRaises(RuntimeError):
                    with InstanceLock(path): pass
            with InstanceLock(path): pass

    def test_env_quotes_preserve_password_without_overwriting_environment(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'EXISTING':'real'},clear=True):
            path = Path(directory)/'.env'
            path.write_text('TEST="a#b!"\nEXISTING=other\n',encoding='utf-8')
            load_env(path)
            self.assertEqual(os.environ['TEST'],'a#b!')
            self.assertEqual(os.environ['EXISTING'],'real')

    def test_invalid_quantities_rejected_before_rendering(self):
        for value in (0,-1,True,'1^XZ','1.5',10001,None):
            with self.subTest(value=value),self.assertRaises(ValueError): quantity({'etiquetas':value})

    def test_daemon_processes_one_payload_and_shuts_down_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'config').mkdir()
            (root/'config/printers.json').write_text(json.dumps({'STORE':{'nome':'Zebra_ZD220','ativo':True}}))
            settings = Settings(root,{'ALLOWED_STORE_KEYS':'STORE','ZEBRA_FEEDBACK_LOCAL_ONLY':'true',
                                     'ZEBRA_PRINTER_NAME':'Zebra_ZD220'})
            settings.port = 0
            stop = threading.Event()
            poll_count = 0
            sample = json.loads((Path(__file__).parent/'fixtures/golden.json').read_text(encoding='utf-8'))[0]['data']
            sample = dict(sample,store_key='STORE')
            def request(url,**kwargs):
                nonlocal poll_count
                poll_count += 1
                if poll_count >= 2: stop.set()
                return {'data':[sample]}
            clock = Clock()
            printer = Printer([snapshot(10),snapshot(11),snapshot(11)])
            agent = Agent(settings,printer,request,now=clock.now,wait=clock.wait)
            run_agent(agent,stop)
            self.assertEqual(len(printer.sent),1)
            self.assertEqual(agent.stats['processed'],1)
