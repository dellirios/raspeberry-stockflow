import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sys
import threading

from . import __version__
from .config import Settings, load_env, load_printers
from .feedback import Monitor
from .formatting import prepare_fields
from .layout import build_zebra_label, build_elgin_label
from .runtime import InstanceLock, run_agent
from .storage import atomic_json, utcnow

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description='Agente StockFlow em Python (Raspberry/Linux)')
    parser.add_argument('--version',action='version',version=__version__)
    parser.add_argument('--root',type=Path,default=ROOT,help='Diretorio de configuracao e logs')
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('run',help='Iniciar agente; consulta fila e pode imprimir')
    sub.add_parser('check',help='Verificar ambiente local; sem rede, USB ou impressao')
    preview = sub.add_parser('preview',help='Gerar ZPL local sem imprimir')
    preview.add_argument('--input',type=Path,help='JSON de uma etiqueta; padrao: exemplo sintetico')
    preview.add_argument('--kind',choices=('zebra','elgin'),default='zebra')
    preview.add_argument('--output',type=Path,help='Arquivo ZPL de saida')
    reader = sub.add_parser('reader',help='Iniciar leitor QR existente; pode efetuar baixas no banco')
    review = sub.add_parser('review',help='Conferencia manual; agente precisa estar parado')
    review.add_argument('action',choices=('list','confirm','release'))
    review.add_argument('id',nargs='?')
    review.add_argument('--reason')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command=='preview':
        source = args.input or ROOT/'examples/label.json'
        data = prepare_fields(json.loads(source.read_text(encoding='utf-8-sig')))
        label = (build_zebra_label if args.kind=='zebra' else build_elgin_label)(data)
        output = args.output or root/'logs/preview/etiqueta-enviada.zpl'
        output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        output.write_text(label['zpl'],encoding='utf-8',newline='\n')
        atomic_json(output.parent/'status.json',dict(kind=args.kind,dpi=203,width=480,height=480,qr=label['qr'],printed=False))
        print('ZPL gerado sem imprimir:',output)
        return 0
    load_env(root/'.env')
    if args.command=='reader':
        load_env(root/'leitor/leitor.env')
        os.environ['STOCKFLOW_ROOT'] = str(root)
        import runpy
        runpy.run_path(str(ROOT/'leitor/leitor.py'),run_name='__main__')
        return 0
    settings = Settings(root)
    if args.command=='check':
        print(json.dumps(dict(version=__version__,python=sys.version.split()[0],platform=sys.platform,
                              config_exists=settings.config_file.is_file(),
                              configured_stores=len(load_printers(settings.config_file)) if settings.config_file.exists() else 0,
                              physical_feedback=settings.modern,token_configured=bool(settings.token),
                              hardware_backend='Linux/CUPS/libusb' if sys.platform.startswith('linux') else 'Indisponivel; core e previews podem ser testados'),ensure_ascii=False,indent=2))
        return 0
    from .linux import LinuxPrinter
    with InstanceLock(settings.logs/'.agent.lock'):
        transport = LinuxPrinter(ROOT)
        if args.command=='review':
            monitor = Monitor(settings.logs/'print-feedback',transport)
            if args.action=='list':
                for record in monitor.records.values():
                    print(record['id'],record['printer'],record['state'],record.get('printed',0),'/',record['expected'])
                return 0
            if not args.reason or not args.reason.strip() or args.id not in monitor.records:
                raise ValueError('Informe uma identidade registrada e --reason com o motivo da conferencia')
            record = monitor.records[args.id]
            if record['state']=='concluido' and args.action=='release':
                raise ValueError('Trabalho concluido nao pode ser liberado para repetir automaticamente')
            if not transport.read()['status']['idle'] or transport.jobs(record['printer'],'not-completed'):
                raise RuntimeError('Impressora precisa estar ociosa e fila vazia')
            if record.get('cupsJobId') and transport.job_state(record['cupsJobId']) not in ('completed','canceled','aborted'):
                raise RuntimeError('Trabalho CUPS ainda nao terminou')
            if record.get('submissionStarted') and not record.get('cupsJobId'):
                raise RuntimeError('Envio incerto sem identificador CUPS; reconciliar a fila antes de revisar o journal')
            record['manualReview'] = dict(user=os.getenv('USER') or os.getenv('USERNAME'),reason=args.reason,action=args.action,at=utcnow())
            if args.action=='release': record['released'] = True
            if args.action=='confirm': record['printed'] = record['expected']
            monitor.change(record,'concluido' if args.action=='confirm' else 'cancelado',['conferencia_manual'])
            print('Conferencia salva; entrega do evento ocorrera ao reiniciar o agente. Nenhum comando foi impresso.')
            return 0
        load_printers(settings.config_file)
        settings.logs.mkdir(parents=True,exist_ok=True,mode=0o700)
        logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',handlers=[
            RotatingFileHandler(settings.logs/'agent.log',maxBytes=1_000_000,backupCount=2,encoding='utf-8'),logging.StreamHandler()])
        stop = threading.Event()
        def shutdown(*unused): stop.set()
        signal.signal(signal.SIGINT,shutdown)
        signal.signal(signal.SIGTERM,shutdown)
        def wait(seconds):
            if stop.wait(seconds):
                raise RuntimeError('Agente encerrando; nao reenviar trabalho incerto')
        if settings.modern and not settings.serial:
            raise ValueError('Defina ZEBRA_USB_SERIAL antes de habilitar confirmacao fisica')
        from .protocol import Client
        from .service import Agent
        agent = Agent(settings,transport,Client(),wait=wait)
        logging.info('StockFlow Python %s iniciado',__version__)
        run_agent(agent,stop)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError,RuntimeError) as error:
        print('Erro:',error,file=sys.stderr)
        raise SystemExit(1)
