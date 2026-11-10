import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stockflow.protocol import validate_target, Outbox, Calibration, Client
from stockflow.config import Settings, load_printers
from stockflow.service import Agent
from test_feedback import Clock, Printer, snapshot
from stockflow.feedback import Monitor


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()

    def test_tokens_only_go_to_exact_official_endpoints(self):
        validate_target('https://stockflow.pro/start.php?print_feedback=1',poll=True,token='token')
        for url in ('http://stockflow.pro/start.php?print_feedback=1','https://evil.test/start.php?print_feedback=1',
                    'https://stockflow.pro@evil.test/start.php?print_feedback=1','https://stockflow.pro/start.php',
                    'https://stockflow.pro:8443/start.php?print_feedback=1','https://stockflow.pro/start.php?print_feedback=1#x'):
            with self.subTest(url=url),self.assertRaises(ValueError): validate_target(url,poll=True,token='token')
        with self.assertRaises(ValueError): validate_target('https://stockflow.pro/status.php',endpoint='/api/print-agent/events')

    def test_outbox_retries_same_event_and_requires_exact_ack(self):
        printer = Printer([snapshot(10),snapshot(11),snapshot(11)])
        monitor = Monitor(self.root/'prints',printer,now=self.clock.now,wait=self.clock.wait)
        monitor.print('Zebra','zpl',['a'],1,[{'id_produto':'TESTE'}])
        calls = []
        def request(url,body=None,**kwargs):
            calls.append(body)
            return {'accepted':True,'event_id':'wrong'}
        outbox = Outbox(monitor,request,'https://stockflow.pro/api/print-agent/events','token','SERIAL')
        with self.assertRaises(ValueError): outbox.deliver()
        first = calls[0]['event_id']
        def accepted(url,body=None,**kwargs):
            calls.append(body)
            return {'accepted':True,'event_id':body['event_id']}
        outbox.request = accepted
        self.assertGreater(outbox.deliver(),0)
        self.assertEqual(calls[1]['event_id'],first)
        self.assertEqual(outbox.deliver(),0)
        self.assertEqual(len(printer.sent),1)

    def test_calibration_is_claimed_once_and_not_replayed(self):
        printer = Printer([snapshot(),snapshot(),snapshot()])
        monitor = Monitor(self.root/'prints',printer,now=self.clock.now,wait=self.clock.wait)
        calls = []
        def report(url,body=None,**kwargs):
            calls.append(body)
            return {'accepted':True,'command_id':'12345678-1234-4234-8234-123456789abc'}
        calibration = Calibration(self.root/'calibration',monitor,report,'token','Zebra',now=self.clock.now,wait=self.clock.wait)
        command = {'id':'12345678-1234-4234-8234-123456789abc','type':'calibrate','state':'aguardando'}
        calibration.process([command])
        calibration.process([command])
        self.assertEqual(printer.sent,['~JC\n'])
        self.assertEqual(calls[0]['state'],'calibrando')
        self.assertEqual(calls[-1]['state'],'concluido')
        self.assertFalse(calibration.is_uncertain())

    def test_calibration_uncertainty_survives_restart(self):
        printer = Printer()
        def fail_submit(*args): raise OSError('lost reply')
        printer.submit = fail_submit
        monitor = Monitor(self.root/'prints',printer)
        def report(url,body=None,**kwargs):
            return {'accepted':True,'command_id':'12345678-1234-4234-8234-123456789abc'}
        command = {'id':'12345678-1234-4234-8234-123456789abc','type':'calibrate','state':'aguardando'}
        Calibration(self.root/'cal',monitor,report,'token','Zebra').process([command])
        recovered = Calibration(self.root/'cal',monitor,report,'token','Zebra')
        self.assertTrue(recovered.is_uncertain())

    def test_new_agent_uses_modern_events_and_filters_other_stores(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/golden.json').read_text(encoding='utf-8'))[0]['data']
        fixture.update(store_key='STORE',print_request_id='12345678-1234-4234-8234-123456789abc',etiquetas=1)
        (self.root/'config').mkdir()
        (self.root/'config/printers.json').write_text(json.dumps({'impressoras':[{'id':'STORE','nome':'Zebra_ZD220','ativo':True}]}))
        settings = Settings(self.root,{'ALLOWED_STORE_KEYS':'STORE','ZEBRA_FEEDBACK_LOCAL_ONLY':'true',
            'ZEBRA_PRINTER_NAME':'Zebra_ZD220','PRINT_FEEDBACK_TOKEN':'token',
            'PRINT_FEEDBACK_EVENTS_URL':'https://stockflow.pro/api/print-agent/events',
            'LONG_POLL_URL':'https://stockflow.pro/start.php?store_key=STORE&print_feedback=1'})
        printer = Printer([snapshot(10),snapshot(11),snapshot(11)])
        requests = []
        def request(url,body=None,**kwargs):
            requests.append(url)
            return {'accepted':True,'event_id':body['event_id']} if body and 'event_id' in body else {'accepted':True}
        agent = Agent(settings,printer,request,now=self.clock.now,wait=self.clock.wait)
        result = agent.process({'data':[dict(fixture,store_key='OTHER'),fixture]})
        self.assertEqual(len(printer.sent),1)
        self.assertEqual(result[0]['status'],'concluido_local')
        agent.process({'data':[fixture]})
        self.assertEqual(len(printer.sent),1)
        agent.outbox.deliver()
        self.assertNotIn('https://stockflow.pro/status.php',requests)

    def test_config_formats_and_invalid_quantity(self):
        path = self.root/'printers.json'
        path.write_text(json.dumps({'impressoras':[{'id':'STORE','nome':'Zebra','ativo':True}]}))
        self.assertEqual(load_printers(path)['STORE']['nome'],'Zebra')
        path.write_text(json.dumps({'STORE':{'nome':'Zebra','ativo':'yes'}}))
        with self.assertRaises(ValueError): load_printers(path)

    def test_http_redirect_is_not_followed_and_connection_is_closed(self):
        class Response:
            status = 302
            will_close = False
            def read(self,*args): return b'{}'
        class Connection:
            def __init__(self): self.sock = None; self.closed = False; self.calls = []
            def request(self,*args): self.calls.append(args)
            def getresponse(self): return Response()
            def close(self): self.closed = True
        connection = Connection()
        with patch('stockflow.protocol.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(RuntimeError):
                Client()('https://stockflow.pro/api/print-agent/heartbeat',body={},token='token')
        self.assertEqual(len(connection.calls),1)
        self.assertTrue(connection.closed)

    def test_heartbeat_can_report_while_printer_is_locked_without_reading_usb(self):
        settings = Settings(self.root,{'ZEBRA_FEEDBACK_LOCAL_ONLY':'true','PRINT_FEEDBACK_TOKEN':'token',
                                      'LONG_POLL_URL':'https://stockflow.pro/start.php?print_feedback=1'})
        printer = Printer()
        sent = []
        def request(url,body=None,**kwargs): sent.append(body); return {'accepted':True}
        agent = Agent(settings,printer,request,now=self.clock.now,wait=self.clock.wait)
        agent.monitor.latest_snapshot = snapshot(10,['tampa_aberta'])
        agent.monitor.latest_at = self.clock.now()
        with agent.monitor.lock:
            with patch.object(printer,'read',side_effect=AssertionError('must not read busy USB')):
                agent.heartbeat_tick()
        self.assertEqual(sent[0]['printer']['state'],'bloqueada')
        self.assertEqual(sent[0]['printer']['reasons'],['tampa_aberta'])

    def test_legacy_duplicate_cache_expires_but_uncertain_jobs_do_not(self):
        settings = Settings(self.root,{})
        agent = Agent(settings,Printer(),lambda *a,**k: {},now=self.clock.now,wait=self.clock.wait)
        agent.monitor.records['old'] = dict(mode='legacy',keys=['legacy-key'],submissionStarted=True,state='concluido',createdAt='2020-01-01T00:00:00Z')
        self.assertFalse(agent.is_duplicate('legacy-key',modern=False))
        self.assertTrue(agent.is_duplicate('legacy-key',modern=True))
        agent.monitor.records['old']['state'] = 'confirmacao_pendente'
        self.assertTrue(agent.is_duplicate('legacy-key',modern=False))
