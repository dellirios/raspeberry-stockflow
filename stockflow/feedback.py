import json
from pathlib import Path
import re
import threading
import time
import uuid

from .storage import atomic_json, utcnow

PHYSICAL_BLOCKS = {'tampa_aberta','pausada','sem_etiqueta','sem_ribbon','temperatura'}


def parse_status(raw):
    frames = [frame.split(',') for frame in re.findall('\x02([^\x03]*)\x03',str(raw))]
    if len(frames) != 3 or len(frames[0]) != 12 or len(frames[1]) != 11 or any(not re.fullmatch('[0-9]+',v) for row in frames for v in row):
        raise ValueError('Resposta ~HS incompleta ou invalida')
    first,second = [list(map(int,frame)) for frame in frames[:2]]
    reasons = [reason for active,reason in [(first[1],'sem_etiqueta'),(second[3],'sem_ribbon'),(second[2],'tampa_aberta'),
               (first[2],'pausada'),(first[10] or first[11],'temperatura'),(first[9],'configuracao_corrompida'),(first[5],'buffer_cheio')] if active]
    return dict(reasons=reasons,formatsPending=first[4],partialFormat=bool(first[7]),labelsRemaining=second[8],
                idle=not reasons and first[4]==0 and first[7]==0 and second[8]==0)


def parse_counter(raw):
    match = re.fullmatch(r'(?:"([0-9]+)"|([0-9]+))',str(raw).strip())
    if not match:
        raise ValueError('Contador ausente ou invalido')
    counter = int(match[1] or match[2])
    if counter > 9007199254740991:
        raise ValueError('Contador fora da faixa segura do protocolo')
    return counter


def completion_evidence(baseline,current,expected,status):
    if any(type(v) is not int or v < 0 or v > 9007199254740991 for v in (baseline,current,expected)) or expected < 1 or current < baseline or current-baseline > expected:
        return 'confirmacao_pendente'
    if status['reasons']:
        return 'bloqueado'
    return 'concluido' if current-baseline == expected and status['idle'] else 'imprimindo'


class FeedbackError(RuntimeError):
    pass


class MaintenanceRequested(FeedbackError):
    pass


class Monitor:
    def __init__(self,directory,transport,now=time.monotonic,wait=time.sleep,timeout=120):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.transport,self.now,self.wait,self.timeout = transport,now,wait,timeout
        self.lock = threading.Lock()
        self.journal_lock = threading.RLock()
        self.records = {}
        self.phase = 'idle'
        self.maintenance_requested = False
        self.latest_snapshot = None
        self.latest_at = None
        for file in sorted(self.directory.glob('*.json')):
            if not re.fullmatch(r'[a-f0-9-]+\.json',file.name):
                continue
            record = json.loads(file.read_text(encoding='utf-8'))
            uuid.UUID(record['id'])
            if record['id']+'.json' != file.name or not isinstance(record['events'],list):
                raise ValueError('Journal invalido; confira antes de iniciar')
            self.records[record['id']] = record
            if record.get('submissionStarted') and record['state'] not in ('concluido','cancelado') and not record.get('released'):
                self.change(record,'confirmacao_pendente',['reinicio_do_agente'])

    def save(self,record):
        with self.journal_lock:
            atomic_json(self.directory/(record['id']+'.json'),record)

    def change(self,record,state,reasons=None,observation=None):
        reasons = reasons or []
        with self.journal_lock:
            if observation:
                self.latest_snapshot,self.latest_at = observation,self.now()
                record['lastObservation'] = dict(observation,observedAt=utcnow())
                record['printed'] = 0 if record.get('baseline') is None else observation['counter']-record['baseline']
            printed = record.get('printed',0)
            if (record.get('state'),record.get('reasons'),record.get('_lastPrinted')) != (state,reasons,printed) or not record['events']:
                record.update(state=state,reasons=reasons,updatedAt=utcnow(),_lastPrinted=printed)
                record['events'].append(dict(sequence=len(record['events'])+1,state=state,reasons=reasons,printed=printed,
                                             at=record['updatedAt'],source='manual' if record.get('manualReview') else 'counter_and_status' if state=='concluido' else 'agent'))
            self.save(record)

    def has_recorded(self,key):
        with self.journal_lock:
            return any(r.get('submissionStarted') and not r.get('released') and key in r['keys'] for r in self.records.values())

    def needs_review(self,printer):
        with self.journal_lock:
            return any(r['printer']==printer and r.get('submissionStarted') and not r.get('released') and r['state'] not in ('concluido','cancelado') for r in self.records.values())

    def summary(self):
        with self.journal_lock:
            records = [r for r in self.records.values() if not r.get('supersededBy')]
            last = max(records,key=lambda r:r.get('updatedAt',''),default=None)
            return dict(jobs=len(records),phase=self.phase,pending_review=sum(r['state']=='confirmacao_pendente' and not r.get('released') for r in records),
                        latest={k:last.get(k) for k in ('id','state','reasons','expected','printed','updatedAt')} if last else None)

    def print(self,printer,zpl,keys,expected,items):
        if type(expected) is not int or not 1 <= expected <= 9007199254740991 or not keys:
            raise ValueError('Quantidade/identidade invalida')
        if not self.lock.acquire(blocking=False):
            raise FeedbackError('Impressora ocupada')
        record = None
        try:
            if self.needs_review(printer) or any(self.has_recorded(k) for k in keys):
                raise FeedbackError('Trabalho registrado ou aguardando conferencia; reenvio bloqueado')
            self.phase = 'preflight'
            with self.journal_lock:
                record = next((r for r in self.records.values() if not r.get('submissionStarted') and not r.get('released') and
                               r['printer']==printer and r['expected']==expected and sorted(r['keys'])==sorted(keys)),None)
                if record is None:
                    record = dict(id=str(uuid.uuid4()),printer=printer,keys=keys,items=items,expected=expected,baseline=None,
                                  state='recebido',reasons=[],submissionStarted=False,events=[],createdAt=utcnow())
                    self.records[record['id']] = record
            self.change(record,'recebido')
            deadline = self.now()+self.timeout
            baseline = None
            while self.now() < deadline:
                if self.maintenance_requested:
                    raise MaintenanceRequested('Pedido suspenso para calibrar')
                try:
                    observation = self.transport.read()
                    status = observation['status']
                    if PHYSICAL_BLOCKS.intersection(status['reasons']):
                        deadline = self.now()+self.timeout
                    self.change(record,'recebido' if status['idle'] else 'bloqueado',status['reasons'] or ([] if status['idle'] else ['impressora_ocupada']),observation)
                    if status['idle'] and not self.transport.jobs(printer,'not-completed'):
                        baseline = observation
                        break
                except (OSError,ValueError,RuntimeError):
                    self.change(record,'bloqueado',['falha_comunicacao'])
                self.wait(.5)
            if baseline is None:
                raise FeedbackError('Impressora indisponivel; nenhum envio realizado')
            if self.maintenance_requested:
                raise MaintenanceRequested('Pedido suspenso para calibrar')
            previous = set(self.transport.jobs(printer))
            record['baseline'] = baseline['counter']
            record['submissionStarted'] = True
            self.phase = 'printing'
            self.change(record,'enviando')
            job = self.transport.submit(printer,zpl)
            if not re.fullmatch(r'[\w.-]+-[0-9]+',str(job)):
                raise FeedbackError('CUPS sem identificador verificavel')
            record['cupsJobId'] = job
            self.change(record,'enviado')
            deadline,stable = self.now()+self.timeout,0
            while self.now() < deadline:
                try:
                    if set(self.transport.jobs(printer)) - previous - {job}:
                        raise FeedbackError('Outro trabalho interferiu no contador')
                    cups_state = self.transport.job_state(job)
                    if cups_state in ('aborted','canceled'):
                        raise FeedbackError('Trabalho CUPS interrompido')
                    observation = self.transport.read()
                    status = observation['status']
                    evidence = completion_evidence(record['baseline'],observation['counter'],expected,status)
                    if evidence == 'confirmacao_pendente':
                        raise FeedbackError('Contador divergente')
                    if PHYSICAL_BLOCKS.intersection(status['reasons']):
                        deadline = self.now()+self.timeout
                    stable = stable+1 if evidence=='concluido' and cups_state=='completed' else 0
                    if stable >= 2:
                        self.change(record,'concluido',[],observation)
                        return dict(id=record['id'],state='concluido',printed=expected,cupsJobId=job)
                    self.change(record,'bloqueado' if evidence=='bloqueado' else 'imprimindo',status['reasons'],observation)
                except FeedbackError:
                    raise
                except (OSError,ValueError,RuntimeError):
                    stable = 0
                    self.change(record,'bloqueado',['falha_comunicacao'])
                self.wait(.5)
            raise FeedbackError('Prazo de verificacao esgotado; conferencia necessaria')
        except Exception as error:
            if record is not None:
                submitted = record['submissionStarted']
                reasons = ['conferencia_necessaria'] if submitted else ['calibracao_solicitada'] if isinstance(error,MaintenanceRequested) else record['reasons'] or ['erro_antes_do_envio']
                self.change(record,'confirmacao_pendente' if submitted else 'bloqueado',reasons)
            if isinstance(error,FeedbackError):
                raise
            raise FeedbackError(str(error)) from error
        finally:
            self.phase = 'idle'
            self.lock.release()
