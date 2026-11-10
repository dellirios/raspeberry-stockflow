import json
from pathlib import Path
import tempfile
import unittest

from stockflow.feedback import Monitor, FeedbackError, parse_status, parse_counter, completion_evidence
from stockflow.storage import atomic_json


def snapshot(counter=10, reasons=None):
    reasons = reasons or []
    return {'counter': counter, 'status': {'idle': not reasons, 'reasons': reasons,
            'formatsPending': 0, 'partialFormat': False, 'labelsRemaining': 0}, 'raw': {}}


class Clock:
    def __init__(self): self.value = 0
    def now(self): return self.value
    def wait(self, seconds): self.value += seconds


class Printer:
    def __init__(self, observations=None):
        self.observations = list(observations or [snapshot()])
        self.sent = []
        self.foreign = False
        self.cups_state = 'completed'
    def read(self):
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]
    def jobs(self, printer, which='all'):
        if not self.sent: return []
        return ['Zebra-12', 'Zebra-13'] if self.foreign else ['Zebra-12']
    def job_state(self, job): return self.cups_state
    def submit(self, printer, zpl):
        self.sent.append(zpl)
        return 'Zebra-12'


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()

    def monitor(self, printer):
        return Monitor(Path(self.temp.name), printer, now=self.clock.now, wait=self.clock.wait, timeout=2)

    def run_print(self, monitor):
        return monitor.print('Zebra', 'zpl', ['key'], 1, [{'id_produto': 'TESTE'}])

    def test_status_parser_is_strict_and_reports_physical_blocks(self):
        first = ['0']*12; second = ['0']*11
        first[1] = first[2] = second[2] = second[3] = '1'
        raw = '\x02'+','.join(first)+'\x03\r\n\x02'+','.join(second)+'\x03\x020,0\x03'
        self.assertEqual(parse_status(raw)['reasons'], ['sem_etiqueta','sem_ribbon','tampa_aberta','pausada'])
        self.assertEqual(parse_counter('"123"\r\n'),123)
        for broken in ('',raw[:-1],raw.replace('0','x')):
            with self.assertRaises(ValueError): parse_status(broken)
        for broken in ('abc','-1','"1"junk','9007199254740992'):
            with self.assertRaises(ValueError): parse_counter(broken)

    def test_only_exact_counter_and_idle_confirm(self):
        status = snapshot()['status']
        self.assertEqual(completion_evidence(10,11,1,status),'concluido')
        self.assertEqual(completion_evidence(10,10,1,status),'imprimindo')
        self.assertEqual(completion_evidence(10,12,1,status),'confirmacao_pendente')
        self.assertEqual(completion_evidence(10,9,1,status),'confirmacao_pendente')

    def test_waits_for_two_idle_readings_and_never_repeats(self):
        printer = Printer([snapshot(10),snapshot(10,['pausada']),snapshot(11),snapshot(11)])
        monitor = self.monitor(printer)
        result = self.run_print(monitor)
        self.assertEqual(result['state'],'concluido')
        self.assertEqual(result['printed'],1)
        self.assertEqual(len(printer.sent),1)
        with self.assertRaises(FeedbackError): self.run_print(monitor)
        self.assertEqual(len(printer.sent),1)

    def test_foreign_job_or_counter_divergence_require_review(self):
        for foreign in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                printer = Printer([snapshot(10),snapshot(12)])
                printer.foreign = foreign
                monitor = Monitor(Path(directory),printer,now=self.clock.now,wait=self.clock.wait,timeout=2)
                with self.assertRaises(FeedbackError): self.run_print(monitor)
                self.assertTrue(monitor.needs_review('Zebra'))
                self.assertEqual(next(iter(monitor.records.values()))['state'],'confirmacao_pendente')

    def test_submission_intent_is_durable_before_cups_call(self):
        printer = Printer([snapshot()])
        monitor = self.monitor(printer)
        def fail(printer_name,zpl):
            files = list(Path(self.temp.name).glob('*.json'))
            self.assertEqual(len(files),1)
            record = json.loads(files[0].read_text())
            self.assertTrue(record['submissionStarted'])
            raise OSError('reply lost')
        printer.submit = fail
        with self.assertRaises(FeedbackError): self.run_print(monitor)
        recovered = self.monitor(printer)
        self.assertTrue(recovered.needs_review('Zebra'))
        with self.assertRaises(FeedbackError): self.run_print(recovered)

    def test_long_physical_block_keeps_same_attempt(self):
        printer = Printer([snapshot(10,['sem_etiqueta']) for _ in range(10)]+[snapshot(10),snapshot(11),snapshot(11)])
        monitor = self.monitor(printer)
        self.assertEqual(self.run_print(monitor)['state'],'concluido')
        self.assertEqual(len(monitor.records),1)
        self.assertEqual(len(printer.sent),1)

    def test_preflight_maintenance_suspends_without_send_and_reuses_id(self):
        printer = Printer([snapshot(10),snapshot(11),snapshot(11)])
        monitor = self.monitor(printer)
        monitor.maintenance_requested = True
        with self.assertRaises(FeedbackError): self.run_print(monitor)
        record_id = next(iter(monitor.records))
        self.assertFalse(printer.sent)
        monitor.maintenance_requested = False
        self.assertEqual(self.run_print(monitor)['id'],record_id)

    def test_corrupt_journal_fails_closed(self):
        (Path(self.temp.name)/'1a3c.json').write_text('{broken')
        with self.assertRaises((ValueError,json.JSONDecodeError)):
            self.monitor(Printer())

    def test_atomic_json_can_be_replaced(self):
        file = Path(self.temp.name)/'state.json'
        atomic_json(file,{'value':1})
        atomic_json(file,{'value':2})
        self.assertEqual(json.loads(file.read_text()),{'value':2})
