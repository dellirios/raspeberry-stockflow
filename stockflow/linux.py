"""Linux-only hardware boundary; importing the portable core never opens USB."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .feedback import parse_status, parse_counter


def run(args,timeout=4,input=None):
    try:
        result = subprocess.run(args,input=input,text=True,encoding='utf-8',capture_output=True,timeout=timeout,
                                env=dict(os.environ,LC_ALL='C'),check=True)
        return result.stdout
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('Prazo esgotado em '+args[0]) from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(args[0]+': '+error.stderr.strip()[:300]) from error


class LinuxPrinter:
    def __init__(self,root):
        if not sys.platform.startswith('linux'):
            raise RuntimeError('Impressao USB/CUPS disponivel nesta etapa somente no Linux; Windows ainda nao implementado')
        self.helper = Path(root)/'scripts/zebra-usb-feedback.py'

    def read(self):
        raw = json.loads(run([sys.executable,str(self.helper)],timeout=6.5))
        return dict(status=parse_status(raw['hostStatus']),counter=parse_counter(raw['counter']),raw=raw)

    def jobs(self,printer,which='all'):
        return [line.split()[0] for line in run(['lpstat','-W',which,'-o',printer]).splitlines() if line.strip()]

    def job_state(self,job):
        if not re.fullmatch(r'[\w.-]+-[0-9]+',job):
            raise ValueError('Identificador CUPS invalido')
        output = run(['ipptool','-tv','ipp://localhost/jobs/'+job.rsplit('-',1)[1],'/usr/share/cups/ipptool/get-job-attributes.test'])
        match = re.search(r'job-state \(enum\) = ([a-z-]+)',output)
        if not match:
            raise ValueError('Estado CUPS indisponivel')
        return match[1]

    def submit(self,printer,zpl):
        args = ['lp','-n','1','-o','raw']
        if printer:
            args += ['-d',printer]
        output = run(args,timeout=3,input=zpl)
        match = re.search(r'request id is ([\w.-]+-[0-9]+)',output)
        if not match:
            raise RuntimeError('CUPS aceitou envio sem identificador; nao repetir')
        return match[1]
