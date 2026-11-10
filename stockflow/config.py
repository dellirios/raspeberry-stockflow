import json
import math
import os
from pathlib import Path
import re


def load_env(path):
    path = Path(path)
    if not path.is_file():
        return
    for number,line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        key,sep,value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key.strip()):
            raise ValueError(f'Linha {number} invalida no arquivo de ambiente')
        value = value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):
            value = value[1:-1]
        os.environ.setdefault(key.strip(),value)


def load_printers(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(raw,dict):
        raise ValueError('Config de impressoras deve ser um objeto')
    if 'impressoras' in raw:
        rows = raw['impressoras']
        if not isinstance(rows,list) or any(not isinstance(row,dict) or not row.get('id') for row in rows):
            raise ValueError('Lista de impressoras invalida')
        raw = {str(row['id']):dict(row) for row in rows}
        if len(raw) != len(rows):
            raise ValueError('Loja duplicada no config')
    for key,printer in raw.items():
        if not isinstance(printer,dict) or type(printer.get('ativo')) is not bool or not (printer.get('nome') or printer.get('impressora')):
            raise ValueError('Config de impressora invalida para '+key)
        printer['nome'] = printer.get('nome') or printer['impressora']
        if not isinstance(printer['nome'],str) or printer['nome'].startswith('-') or not re.fullmatch(r'[\w.-]+',printer['nome']):
            raise ValueError('Nome de fila CUPS invalido')
    return raw


class Settings:
    def __init__(self,root,env=None):
        env = os.environ if env is None else env
        self.root = Path(root).resolve()
        self.config_file = self.root/'config/printers.json'
        self.logs = self.root/'logs'
        self.allowed = {v.strip() for v in env.get('ALLOWED_STORE_KEYS','').split(',') if v.strip()}
        self.token = env.get('PRINT_FEEDBACK_TOKEN','')
        self.poll_url = env.get('LONG_POLL_URL','https://stockflow.pro/start.php')
        self.events_url = env.get('PRINT_FEEDBACK_EVENTS_URL','')
        self.modern = env.get('ZEBRA_FEEDBACK_LOCAL_ONLY','false').lower()=='true'
        self.printer = env.get('ZEBRA_PRINTER_NAME','Zebra_Technologies_ZTC_ZD220-203dpi_ZPL')
        self.serial = env.get('ZEBRA_USB_SERIAL','')
        self.host = env.get('HOST','127.0.0.1')
        self.port = int(env.get('PORT','2508'))
        if not 1 <= self.port <= 65535:
            raise ValueError('Porta local invalida')
        self.empty_delay = self.seconds(env,'EMPTY_POLL_BASE_DELAY_MS',3000)
        self.noop_delay = self.seconds(env,'NOOP_PAYLOAD_BASE_DELAY_MS',5000)
        self.max_delay = self.seconds(env,'MAX_IDLE_BACKOFF_MS',20000)

    @staticmethod
    def seconds(env,key,default):
        seconds = float(env.get(key,str(default)))/1000
        if not math.isfinite(seconds) or not .1 <= seconds <= 300:
            raise ValueError(key+' deve estar entre 100 e 300000 ms')
        return seconds
