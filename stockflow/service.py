import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from .config import load_printers
from .feedback import Monitor, FeedbackError
from .formatting import prepare_fields
from .layout import build_zebra_label, build_elgin_label, apply_settings, escape_data
from .protocol import Outbox, Calibration, validate_target, ORIGIN
from .storage import utcnow

LOG = logging.getLogger(__name__)


def quantity(data):
    value = data.get('etiquetas',1)
    if isinstance(value,bool) or not re.fullmatch(r'[0-9]+',str(value)) or not 1 <= int(value) <= 10000:
        raise ValueError('Quantidade deve ser um inteiro entre 1 e 10000')
    return int(value)


def job_key(data):
    identity = data.get('print_request_id') or data.get('print_batch_uuid') or data.get('label_code') or ''
    critical = f"{data.get('store_key')}|{data.get('id_produto')}|{quantity(data)}|{data.get('nome')}|{identity}"
    return hashlib.sha1(critical.encode('utf-8')).hexdigest()


def render(data,printer):
    fields = prepare_fields(data)
    name = printer['nome']
    if name=='ELGIN_L42PRO':
        zpl = build_elgin_label(fields)['zpl']
    elif re.search(r'ZD220|GC420t',name,re.I):
        zpl = build_zebra_label(fields)['zpl']
    else:
        words = str(fields.get('nome') or '').split(' ')
        first,second = [],[]
        for word in words:
            if len(' '.join(first+[word]))<=36 and len(first)<7:
                first.append(word)
            else:
                second.append(word)
        fields['nome'],fields['nome_2'] = ' '.join(first),' '.join(second)
        template = (Path(__file__).resolve().parent.parent/'templates/default.zpl').read_text(encoding='utf-8')
        zpl = re.sub(r'\{([^}]+)\}',lambda m:escape_data(str(fields.get(m[1]) or '')),template)
    return apply_settings(zpl.replace('^PQ1,0,1,Y',f'^PQ{quantity(data)},0,1,Y'),printer)


class Agent:
    def __init__(self,settings,transport,request,now=time.monotonic,wait=time.sleep):
        self.settings,self.transport,self.request = settings,transport,request
        self.now,self.wait = now,wait
        validate_target(settings.poll_url,poll=True,token=settings.token)
        if settings.token and not settings.modern:
            raise ValueError('Token moderno exige confirmacao fisica habilitada')
        self.monitor = Monitor(settings.logs/'print-feedback',transport,now=now,wait=wait)
        self.outbox = Outbox(self.monitor,request,settings.events_url,settings.token,settings.serial)
        self.calibration = Calibration(settings.logs/'printer-calibration',self.monitor,request,settings.token,settings.printer,now=now,wait=wait) if settings.modern and settings.token else None
        self.lock = threading.Lock()
        self.started = now()
        self.stats = dict(processed=0,duplicates=0,failures=0,skipped=0)
        self.heartbeat = dict(enabled=bool(settings.token),last_success_at=None,last_error=None)
        self.daemon_failures = 0

    def modern(self,printer):
        return self.settings.modern and printer['nome']==self.settings.printer

    def is_duplicate(self,key,modern):
        with self.monitor.journal_lock:
            for record in self.monitor.records.values():
                if not record.get('submissionStarted') or record.get('released') or key not in record['keys']:
                    continue
                if not modern and record.get('mode')=='legacy' and record['state']=='concluido':
                    created = datetime.fromisoformat(record['createdAt'].replace('Z','+00:00'))
                    if (datetime.now(timezone.utc)-created).total_seconds()>900:
                        continue
                return True
        return False

    def process(self,payload):
        if not isinstance(payload,dict) or not isinstance(payload.get('data'),list) or len(payload['data'])>1000 or any(not isinstance(e,dict) for e in payload['data']):
            raise ValueError('JSON de etiquetas malformado ou maior que 1000 itens')
        if not self.lock.acquire(blocking=False):
            return dict(status='busy',message='Processamento em andamento')
        try:
            if self.calibration and self.calibration.is_uncertain():
                return dict(status='busy',message='Confira a calibracao antes de imprimir')
            printers = load_printers(self.settings.config_file)
            groups = {}
            for item in payload['data']:
                store = item.get('store_key')
                if not store or (self.settings.allowed and store not in self.settings.allowed) or store not in printers or not printers[store]['ativo']:
                    self.stats['skipped'] += 1
                    continue
                printer = printers[store]
                if self.settings.token and not self.modern(printer):
                    self.stats['skipped'] += 1
                    LOG.warning('Fila nao corresponde a impressora com feedback; pedido moderno bloqueado')
                    continue
                if self.modern(printer) and self.settings.events_url:
                    try:
                        uuid.UUID(item.get('print_request_id',''))
                    except (ValueError,TypeError,AttributeError):
                        self.stats['skipped'] += 1
                        LOG.warning('Pedido sem identidade moderna valida; impressao bloqueada')
                        continue
                key = job_key(item)
                if self.is_duplicate(key,self.modern(printer)):
                    self.stats['duplicates'] += 1
                    continue
                group = groups.setdefault((store,printer['nome']),dict(printer=printer,items=[],keys=[]))
                if key not in group['keys']:
                    group['items'].append(item)
                    group['keys'].append(key)
            results = []
            for group in groups.values():
                printer,items,keys = group['printer'],group['items'],group['keys']
                try:
                    zpl = '\n'.join(render(item,printer) for item in items)
                    if self.modern(printer):
                        identities = [dict(id_produto=str(e['id_produto']),store_key=e['store_key'],print_request_id=e.get('print_request_id'),
                                           print_batch_uuid=e.get('print_batch_uuid'),label_code=e.get('label_code')) for e in items]
                        result = self.monitor.print(printer['nome'],zpl,keys,sum(quantity(e) for e in items),identities)
                        results.extend(dict(id=e['id_produto'],store_key=e['store_key'],status='concluido_local',mensagem='Zebra confirmou a impressao',attempt_id=result['id']) for e in items)
                    else:
                        self.print_legacy(printer,zpl,keys,items)
                        results.extend(dict(id=e['id_produto'],store_key=e['store_key'],status='impresso',mensagem='Aceito pelo CUPS; modo legado sem prova fisica') for e in items)
                    self.stats['processed'] += len(items)
                except (OSError,ValueError,RuntimeError) as error:
                    self.stats['failures'] += len(items)
                    LOG.warning('Lote nao concluido: %s',error)
                    results.extend(dict(id=e.get('id_produto'),store_key=e['store_key'],status='erro',mensagem=str(error)) for e in items)
            return results
        finally:
            self.lock.release()

    def print_legacy(self,printer,zpl,keys,items):
        with self.monitor.lock:
            if self.monitor.needs_review(printer['nome']):
                raise FeedbackError('Trabalho anterior aguardando conferencia')
            record = dict(id=str(uuid.uuid4()),mode='legacy',printer=printer['nome'],keys=keys,items=items,expected=sum(quantity(e) for e in items),
                          baseline=None,state='enviando',reasons=[],events=[],submissionStarted=True,createdAt=utcnow(),legacyAcked=[])
            with self.monitor.journal_lock:
                self.monitor.records[record['id']] = record
            self.monitor.change(record,'enviando')
            try:
                record['cupsJobId'] = self.transport.submit(printer['nome'],zpl)
                record['cupsAccepted'] = True
                self.monitor.change(record,'concluido')
            except Exception:
                self.monitor.change(record,'confirmacao_pendente',['conferencia_necessaria'])
                raise
        self.flush_legacy()

    def flush_legacy(self):
        with self.monitor.journal_lock:
            records = list(self.monitor.records.values())
        for record in records:
            if record.get('mode') != 'legacy' or not record.get('cupsAccepted'):
                continue
            for item in record['items']:
                product = str(item['id_produto'])
                if product in record['legacyAcked']:
                    continue
                self.request(ORIGIN+'/status.php',body=dict(status='concluido',id_produto=item['id_produto']),timeout=3)
                with self.monitor.journal_lock:
                    record['legacyAcked'].append(product)
                    self.monitor.save(record)

    def process_commands(self,commands):
        if not self.calibration or not self.lock.acquire(blocking=False):
            return
        try:
            self.calibration.process(commands)
        finally:
            self.monitor.maintenance_requested = False
            self.lock.release()

    def telemetry(self):
        phase = self.monitor.phase
        if phase=='calibrating':
            return dict(state='calibrando',reasons=[],age_seconds=0)
        if self.monitor.lock.acquire(blocking=False):
            try:
                observation = self.transport.read() if self.settings.modern else None
                if observation:
                    self.monitor.latest_snapshot,self.monitor.latest_at = observation,self.now()
            except (OSError,ValueError,RuntimeError) as error:
                disconnected = 'desconectada' in str(error)
                return dict(state='desconectada' if disconnected else 'indisponivel',reasons=[] if disconnected else ['falha_comunicacao'],age_seconds=0)
            finally:
                self.monitor.lock.release()
        observation = self.monitor.latest_snapshot
        if observation:
            status = observation['status']
            state = 'bloqueada' if status['reasons'] else 'imprimindo' if phase=='printing' or not status['idle'] else 'pronta'
            return dict(state=state,reasons=status['reasons'],age_seconds=min(86400,max(0,int(self.now()-self.monitor.latest_at))))
        return dict(state='indisponivel',reasons=[],age_seconds=0)

    def heartbeat_tick(self):
        if not self.settings.token:
            return
        try:
            ack = self.request(ORIGIN+'/api/print-agent/heartbeat',body={'printer':self.telemetry()},token=self.settings.token)
            if ack.get('accepted') is not True:
                raise ValueError('Heartbeat nao aceito')
            self.heartbeat.update(last_success_at=utcnow(),last_error=None)
        except Exception as error:
            self.heartbeat['last_error'] = str(error)

    def health(self):
        return dict(status='ok',runtime='python',uptime=int(self.now()-self.started),stats=dict(self.stats),
                    printer_feedback=self.monitor.summary(),event_delivery=dict(self.outbox.state),agent_heartbeat=dict(self.heartbeat),
                    daemon_failures=self.daemon_failures)
