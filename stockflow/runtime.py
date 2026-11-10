import collections
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import random
import threading
import time
from urllib.parse import urlsplit, parse_qs

from .protocol import ORIGIN, COMMANDS, validate_target

LOG = logging.getLogger(__name__)


class InstanceLock:
    def __init__(self,path): self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.file = self.path.open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                self.file.seek(0,2)
                if self.file.tell()==0:
                    self.file.write(b'\0'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise RuntimeError('Outro agente esta em execucao; pare o servico antes de continuar') from error
        return self

    def __exit__(self,*args):
        if os.name=='nt':
            import msvcrt
            self.file.seek(0); msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
        self.file.close()


def polling_delay(settings,cycles,noop,random_value=None):
    value = random.random() if random_value is None else random_value
    base = settings.noop_delay if noop else settings.empty_delay
    growth = 1.6 if noop else 1.4
    return min(settings.max_delay,base*growth**max(0,min(cycles,8)-1))*(.7+.6*value)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self,*args,**kwargs):
        self.slots = threading.BoundedSemaphore(4)
        self.rate = collections.deque()
        self.rate_lock = threading.Lock()
        super().__init__(*args,**kwargs)

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request,address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self,request,address):
        try:
            request.settimeout(5)
            super().process_request_thread(request,address)
        finally:
            self.slots.release()


def create_server(agent,host,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass

        def send(self,status,body,kind='application/json; charset=utf-8'):
            raw = json.dumps(body,ensure_ascii=False).encode('utf-8') if kind.startswith('application/json') else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Connection','close')
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

        def do_GET(self):
            if len(self.path)>2048:
                return self.send(414,{'error':'Endereco longo demais'})
            now = time.monotonic()
            with self.server.rate_lock:
                while self.server.rate and self.server.rate[0]<now-60:
                    self.server.rate.popleft()
                limited = len(self.server.rate)>=120
                if not limited: self.server.rate.append(now)
            if limited:
                return self.send(429,{'error':'Limite de consultas locais atingido'})
            parsed = urlsplit(self.path)
            try:
                if parsed.path=='/health':
                    return self.send(200,agent.health())
                if parsed.path=='/metrics':
                    health = agent.health()
                    metrics = [('stockflow_uptime_seconds',health['uptime'])]+[(f'stockflow_{key}_total',value) for key,value in health['stats'].items()]
                    return self.send(200,'\n'.join(f'{key} {value}' for key,value in metrics)+'\n','text/plain; charset=utf-8')
                if parsed.path=='/imprimir-etiqueta/health':
                    return self.send(200,dict(status='ok',service='etiquetas-api'))
                if parsed.path.rstrip('/')=='/imprimir-etiqueta':
                    query = parse_qs(parsed.query)
                    if set(query)-{'url'} or len(query.get('url',[]))>1:
                        raise ValueError('Parametros invalidos')
                    target = query.get('url',[agent.settings.poll_url])[0]
                    # The local route may only fetch its configured queue, never another store or arbitrary host.
                    if target != agent.settings.poll_url:
                        raise ValueError('Use a fila configurada neste dispositivo')
                    validate_target(target,poll=True,token=agent.settings.token)
                    payload = agent.request(target,token=agent.settings.token,timeout=65)
                    return self.send(200,dict(status='success',data=agent.process(payload)))
                if parsed.path.startswith('/previa-etiqueta/'):
                    name = parsed.path.rsplit('/',1)[1]
                    if name in ('etiqueta-enviada.zpl','status.json'):
                        file = agent.settings.logs/'preview'/name
                        if file.is_file():
                            return self.send(200,file.read_text(encoding='utf-8'),'text/plain; charset=utf-8')
                return self.send(404,{'error':'Nao encontrado'})
            except ValueError as error:
                return self.send(400,{'error':str(error)})
            except Exception:
                LOG.warning('Falha na consulta HTTP local',exc_info=True)
                return self.send(502,{'error':'Falha ao consultar ou processar a fila'})

    return LocalServer((host,port),Handler)


def run_agent(agent,stop):
    settings = agent.settings
    server = create_server(agent,settings.host,settings.port)

    def event_worker():
        failures = 0
        while not stop.is_set():
            try:
                agent.outbox.deliver()
                if agent.calibration:
                    agent.calibration.sync()
                agent.flush_legacy()
                failures = 0
            except Exception as error:
                failures += 1
                agent.outbox.state['last_error'] = str(error)
                LOG.warning('Entrega de status pendente; nao sera repetida a impressao')
            stop.wait(min(60,2**min(failures,6)) if failures else 1)

    def heartbeat_worker():
        while not stop.is_set():
            agent.heartbeat_tick()
            stop.wait(10)

    def commands_worker():
        while not stop.wait(2):
            if agent.calibration and agent.monitor.phase=='preflight' and not agent.monitor.maintenance_requested:
                try:
                    payload = agent.request(ORIGIN+COMMANDS,token=settings.token,timeout=3)
                    if isinstance(payload,dict) and any(c.get('type')=='calibrate' for c in payload.get('commands',[])):
                        agent.monitor.maintenance_requested = True
                except Exception:
                    LOG.debug('Consulta de calibracao indisponivel')

    targets = [server.serve_forever,event_worker,commands_worker]
    if settings.token: targets.append(heartbeat_worker)
    workers = [threading.Thread(target=target,daemon=True,name=target.__name__) for target in targets]
    for worker in workers: worker.start()
    cycles,failures,last_failure = 0,0,0
    try:
        while not stop.is_set():
            if failures>=3 and time.monotonic()-last_failure<30:
                stop.wait(1)
                continue
            try:
                payload = agent.request(settings.poll_url,token=settings.token,timeout=65)
                if stop.is_set():
                    break
                if not isinstance(payload,dict):
                    raise ValueError('Payload de polling invalido')
                agent.process_commands(payload.get('commands',[]))
                items = payload.get('data',[])
                results = agent.process(payload) if items else []
                completed = isinstance(results,list) and any(r['status'] in ('impresso','concluido_local') for r in results)
                failures = 0
                cycles = 0 if completed else min(8,cycles+1)
                stop.wait(.1 if completed else polling_delay(settings,cycles,bool(items)))
            except Exception as error:
                if stop.is_set(): break
                failures += 1
                last_failure = time.monotonic()
                LOG.warning('Consulta/processamento indisponivel (%s)',type(error).__name__)
                stop.wait(min(30,2**min(failures,5)))
            agent.daemon_failures = failures
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        for worker in workers:
            worker.join(1)
