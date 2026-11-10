"""Existing StockFlow API contracts; no redirects or bearer-token forwarding."""
import copy
import http.client
import json
import logging
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.parse import urlsplit, parse_qs
import uuid

from .storage import atomic_json, utcnow

ORIGIN = 'https://stockflow.pro'
COMMANDS = '/api/print-agent/commands'
LOG = logging.getLogger(__name__)


def validate_target(url,endpoint=None,poll=False,token=''):
    parts = urlsplit(url)
    if parts.scheme!='https' or parts.hostname!='stockflow.pro' or parts.port not in (None,443) or parts.username or parts.password or parts.fragment:
        raise ValueError('Destino da API nao autorizado')
    if endpoint is not None and (parts.path != endpoint or parts.query):
        raise ValueError('Endpoint da API nao autorizado')
    if poll and (parts.path != '/start.php' or (token and parse_qs(parts.query).get('print_feedback') != ['1'])):
        raise ValueError('Polling moderno requer /start.php e print_feedback=1')
    return parts


class Client:
    def __init__(self):
        self.local = threading.local()
        self.context = ssl.create_default_context()

    def __call__(self,url,body=None,token='',timeout=5):
        parts = validate_target(url)
        headers = {'Accept':'application/json','User-Agent':'StockFlow-Python/2.1.0'}
        if token:
            headers['Authorization'] = 'Bearer '+token
        payload = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            payload = json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8')
        connection = getattr(self.local,'connection',None)
        if connection is None:
            connection = http.client.HTTPSConnection('stockflow.pro',timeout=timeout,context=self.context)
            self.local.connection = connection
        connection.timeout = timeout
        if connection.sock:
            connection.sock.settimeout(timeout)
        try:
            connection.request('POST' if body is not None else 'GET',parts.path+('?' + parts.query if parts.query else ''),payload,headers)
            response = connection.getresponse()
            raw = response.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:
                raise ValueError('Resposta da API excede 4 MB')
            if not 200 <= response.status < 300:
                raise RuntimeError(f'API HTTP {response.status}; resposta nao aceita')
            if response.will_close:
                connection.close()
                self.local.connection = None
            def reject_constant(value):
                raise ValueError('Numero JSON invalido')
            return json.loads(raw.decode('utf-8-sig'),parse_constant=reject_constant) if raw else {}
        except Exception:
            connection.close()
            self.local.connection = None
            raise


class Outbox:
    def __init__(self,monitor,request,url,token,serial):
        self.monitor,self.request,self.url,self.token,self.serial = monitor,request,url,token,serial
        self.state = dict(configured=bool(url and token),last_success_at=None,last_error=None)
        if url:
            validate_target(url,endpoint='/api/print-agent/events')
            if not token:
                raise ValueError('API de eventos exige token do dispositivo')

    def deliver(self):
        if not self.state['configured']:
            return 0
        delivered = 0
        with self.monitor.journal_lock:
            records = copy.deepcopy(list(self.monitor.records.values()))
        for record in records:
            if not record.get('items') or record.get('supersededBy') or record.get('mode')=='legacy':
                continue
            for event in record['events']:
                if event['sequence'] <= record.get('ackedSequence',0):
                    continue
                body = dict(event_id=f"{record['id']}:{event['sequence']}",attempt_id=record['id'],
                            expected=record['expected'],printer_serial=self.serial,printer_name=record['printer'],
                            items=record['items'],cups_job_id=record.get('cupsJobId'),**event)
                ack = self.request(self.url,body=body,token=self.token,timeout=3)
                if ack.get('accepted') is not True or ack.get('event_id') != body['event_id']:
                    raise ValueError('API nao confirmou o evento esperado')
                with self.monitor.journal_lock:
                    current = self.monitor.records[record['id']]
                    current['ackedSequence'] = max(event['sequence'],current.get('ackedSequence',0))
                    self.monitor.save(current)
                delivered += 1
        self.state.update(last_error=None)
        if delivered:
            self.state['last_success_at'] = utcnow()
        return delivered


class Calibration:
    def __init__(self,directory,monitor,request,token,printer,now=time.monotonic,wait=time.sleep,timeout=60):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.monitor,self.request,self.token,self.printer = monitor,request,token,printer
        self.now,self.wait,self.timeout = now,wait,timeout
        self.records = {}
        self.lock = threading.RLock()
        for file in self.directory.glob('*.json'):
            uuid.UUID(file.stem)
            record = json.loads(file.read_text(encoding='utf-8'))
            if record['id'] != file.stem:
                raise ValueError('Journal de calibracao invalido')
            self.records[record['id']] = record
            if record['state'] not in ('concluido','erro','confirmacao_pendente'):
                record.update(state='confirmacao_pendente',message='Agente reiniciado durante a calibracao; confira a impressora.',reported=False)
                self.save(record)

    def save(self,record):
        with self.lock:
            atomic_json(self.directory/(record['id']+'.json'),record)

    def is_uncertain(self):
        with self.lock:
            for record in sorted(self.records.values(),key=lambda r:r['createdAt'],reverse=True):
                if record['state']=='concluido': return False
                if record['state']=='confirmacao_pendente': return True
        return False

    def report(self,record):
        ack = self.request(ORIGIN+COMMANDS+'/'+record['id'],body=dict(state=record['state'],message=record['message']),token=self.token)
        if ack.get('accepted') is not True or ack.get('command_id') != record['id']:
            raise ValueError('Servidor nao confirmou a calibracao esperada')
        record['reported'] = True
        self.save(record)

    def sync(self):
        with self.lock:
            for record in self.records.values():
                if record['state'] in ('concluido','erro','confirmacao_pendente') and not record.get('reported'):
                    self.report(record)

    def process(self,commands):
        if not commands:
            return
        if not isinstance(commands,list):
            raise ValueError('Lista de comandos invalida')
        command = commands[0]
        identity = command.get('id','')
        if not re.fullmatch(r'[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}',identity) or command.get('type')!='calibrate' or command.get('state') not in ('aguardando','calibrando'):
            raise ValueError('Comando de impressora invalido')
        with self.lock:
            if identity in self.records:
                return
        if not self.monitor.lock.acquire(blocking=False):
            return
        record = dict(id=identity,state='calibrando',message='Calibrando os sensores da impressora.',reported=False,createdAt=utcnow())
        with self.lock:
            self.records[identity] = record
        submitted = False
        try:
            if command['state'] != 'aguardando':
                raise RuntimeError('Comando assumido anteriormente; confira a impressora')
            if self.monitor.needs_review(self.printer) or self.monitor.transport.jobs(self.printer,'not-completed'):
                raise RuntimeError('Existe impressao em andamento ou aguardando conferencia')
            self.monitor.phase = 'calibrating'
            before = self.monitor.transport.read()['status']
            if set(before['reasons'])-{'sem_etiqueta','pausada'} or before['formatsPending'] or before['partialFormat'] or before['labelsRemaining']:
                raise RuntimeError('Feche a tampa, confira o ribbon e aguarde a impressao terminar')
            self.save(record)
            self.report(record)
            previous = set(self.monitor.transport.jobs(self.printer))
            submitted = True
            record['submissionStarted'] = True
            self.save(record)
            job = self.monitor.transport.submit(self.printer,'~JC\n')
            if not re.fullmatch(r'[\w.-]+-[0-9]+',str(job)):
                raise RuntimeError('Calibracao sem identificador CUPS')
            record['cupsJobId'] = job
            self.save(record)
            self.wait(1.5)
            deadline,stable = self.now()+self.timeout,0
            while self.now()<deadline:
                if set(self.monitor.transport.jobs(self.printer))-previous-{job}:
                    raise RuntimeError('Outro trabalho durante a calibracao')
                state = self.monitor.transport.job_state(job)
                if state in ('canceled','aborted'):
                    raise RuntimeError('Calibracao interrompida pelo CUPS')
                observation = self.monitor.transport.read()
                self.monitor.latest_snapshot,self.monitor.latest_at = observation,self.now()
                stable = stable+1 if state=='completed' and observation['status']['idle'] else 0
                if stable>=2:
                    record.update(state='concluido',message='Rotina de calibracao concluida. Confira o alinhamento da proxima etiqueta.')
                    break
                self.wait(.5)
            if record['state']!='concluido':
                raise RuntimeError('Sem confirmacao do fim da calibracao')
        except Exception as error:
            record.update(state='confirmacao_pendente' if submitted or command['state']!='aguardando' else 'erro',message=str(error)[:255])
        finally:
            record['reported'] = False
            self.save(record)
            self.monitor.phase = 'idle'
            self.monitor.maintenance_requested = False
            self.monitor.lock.release()
        try:
            self.report(record)
        except (OSError,ValueError,RuntimeError,http.client.HTTPException):
            LOG.warning('Resultado de calibracao persistido; entrega pendente')
