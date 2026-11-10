#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Serviço Python para automação da baixa de produtos via leitor QR Code
Autor: Sistema Stockflow
Descrição: Serviço independente e resiliente para processamento de produtos
"""

try:
    import mysql.connector
    from mysql.connector import pooling, Error as MySQLError
    MYSQL_AVAILABLE = True
except ImportError:
    mysql = None
    pooling = None
    MYSQL_AVAILABLE = False
    MySQLError = Exception
    print("AVISO: mysql-connector não está disponível. Instale com: apt install python3-mysql.connector")
import json
from pathlib import Path
import os
import time
import logging
from logging.handlers import TimedRotatingFileHandler
import threading
from queue import Queue, Empty
from collections import deque
import signal
import sys
from datetime import datetime
from decimal import Decimal
import re

# Importações para monitoramento de eventos de teclado
try:
    import evdev
    from evdev import InputDevice, categorize, ecodes
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
    print("AVISO: evdev não está disponível. Instale com: pip install evdev")

BASE_DIR = Path(os.getenv("STOCKFLOW_ROOT", str(Path(__file__).resolve().parent.parent))).resolve()
STATE_DIR = BASE_DIR / "logs" / "leitor"

class StockflowQRService:
    def __init__(self):
        self.running = False
        self.store_key = None
        self.store_keys = []
        self.db_pool = None
        self.job_queue = deque()
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.job_file = str(STATE_DIR / "jobs.json")
        self.config_file = str(BASE_DIR / "config" / "printers.json")
        self.input_device = None
        self.current_input = ""
        self.device_config_file = str(BASE_DIR / "config" / "device_config.json")
        self.preferred_device = None
        
        # Configuração de logging
        self.setup_logging()
        
        # Configuração do banco de dados
        self.db_config = {
            "host": os.getenv("QRCODE_DB_HOST", "localhost"),
            "port": int(os.getenv("QRCODE_DB_PORT", "3306")),
            "user": os.getenv("QRCODE_DB_USER", "root"),
            "password": os.getenv("QRCODE_DB_PASSWORD", ""),
            "database": os.getenv("QRCODE_DB_NAME", "flow"),
            "charset": "utf8mb4",
            "autocommit": False,
            "pool_name": "stockflow_pool",
            "pool_size": 1,
            "use_pure": True,
            "pool_reset_session": True
        }
        self.force_device_path = os.getenv("LEITOR_DEVICE_PATH", "").strip()
        
        # Thread para processamento da fila
        self.queue_processor_thread = None
        self.db_reconnect_thread = None
        self.db_connected = False
        self.last_device_scan = 0
        self.device_scan_interval = 30  # Escaneia dispositivos a cada 30 segundos
        
        # Configuração de sinais para encerramento gracioso
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def setup_logging(self):
        """Configura o sistema de logging estruturado"""
        log_format = '[%(asctime)s] %(levelname)s: %(message)s'
        log_file = str(STATE_DIR / "leitor.log")
        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            handlers=[
                TimedRotatingFileHandler(
                    log_file,
                    when='midnight',
                    interval=1,
                    backupCount=1,
                    encoding='utf-8'
                ),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("Sistema de logging inicializado")
    
    def load_configuration(self):
        """Carrega a configuração do arquivo printers.json"""
        try:
            if not os.path.exists(self.config_file):
                self.logger.error(f"Arquivo de configuração não encontrado: {self.config_file}")
                return False
            
            if not os.access(self.config_file, os.R_OK):
                self.logger.error(f"Sem permissão de leitura para: {self.config_file}")
                return False
            
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            store_keys = []

            # Formato legado: {"store_key":"..."}
            # Formato atual do Stockflow: {"impressoras":[{"id":"..."}]}
            if 'store_key' in config and config['store_key']:
                store_keys.append(str(config['store_key']).strip())

            impressoras = config.get('impressoras')
            if isinstance(impressoras, list):
                for impressora in impressoras:
                    if not isinstance(impressora, dict):
                        continue
                    if impressora.get('ativo') is False:
                        continue

                    candidate = impressora.get('id') or impressora.get('store_key')
                    if candidate:
                        store_keys.append(str(candidate).strip())

            for key, value in config.items():
                if key in ('store_key', 'store_name', 'qr_code_settings', 'impressoras'):
                    continue
                if isinstance(value, dict) and value.get('ativo') is not False:
                    store_keys.append(str(key).strip())

            self.store_keys = list(dict.fromkeys(key for key in store_keys if key))
            self.store_key = self.store_keys[0] if self.store_keys else None

            if not self.store_keys:
                self.logger.error("store_key não encontrado no arquivo de configuração")
                return False

            self.logger.info(
                "Configuração carregada. Store Keys: %s",
                ', '.join(self.store_keys)
            )
            
            # Carrega configuração do dispositivo preferido
            self.load_device_config()
            
            return True
            
        except json.JSONDecodeError as e:
            self.logger.error(f"Erro ao decodificar JSON do arquivo de configuração: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Erro ao carregar configuração: {e}")
            return False
    
    def load_device_config(self):
        """Carrega configuração do dispositivo preferido"""
        try:
            if os.path.exists(self.device_config_file):
                with open(self.device_config_file, 'r', encoding='utf-8') as file:
                    device_config = json.load(file)
                    self.preferred_device = device_config.get('preferred_device')
                    if self.preferred_device:
                        self.logger.info(f"Dispositivo preferido carregado: {self.preferred_device['name']} ({self.preferred_device['path']})")
        except Exception as e:
            self.logger.warning(f"Erro ao carregar configuração do dispositivo: {e}")
            self.preferred_device = None
    
    def save_device_config(self, device):
        """Salva configuração do dispositivo que funcionou"""
        try:
            device_config = {
                'preferred_device': {
                    'name': device.name,
                    'path': device.path,
                    'vendor_id': getattr(device.info, 'vendor', None),
                    'product_id': getattr(device.info, 'product', None),
                    'last_used': datetime.now().isoformat()
                }
            }
            
            # Cria o diretório se não existir
            os.makedirs(os.path.dirname(self.device_config_file), exist_ok=True)
            
            with open(self.device_config_file, 'w', encoding='utf-8') as file:
                json.dump(device_config, file, indent=2, ensure_ascii=False)
            
            self.preferred_device = device_config['preferred_device']
            self.logger.info(f"Configuração do dispositivo salva: {device.name}")
            
        except Exception as e:
            self.logger.warning(f"Erro ao salvar configuração do dispositivo: {e}")
    
    def setup_database_pool(self):
        """Configura o pool de conexões com o banco de dados"""
        if not MYSQL_AVAILABLE:
            self.logger.error("Dependência ausente: mysql-connector-python")
            self.db_connected = False
            return False

        try:
            self.logger.info(
                "Conectando ao MySQL em %s:%s (db=%s, user=%s)",
                self.db_config.get("host"),
                self.db_config.get("port"),
                self.db_config.get("database"),
                self.db_config.get("user"),
            )
            self.db_pool = pooling.MySQLConnectionPool(**self.db_config)
            self.db_connected = True
            self.logger.info("Pool de conexões MySQL criado com sucesso")
            return True
        except MySQLError as e:
            self.logger.error(f"Erro ao criar pool de conexões MySQL: {e}")
            self.db_connected = False
            return False
    
    def get_db_connection(self):
        """Obtém uma conexão do pool"""
        try:
            if self.db_pool:
                return self.db_pool.get_connection()
            return None
        except MySQLError as e:
            self.logger.error(f"Erro ao obter conexão do pool: {e}")
            self.db_connected = False
            return None
    
    def load_pending_jobs(self):
        """Carrega trabalhos pendentes do arquivo de persistência"""
        try:
            if os.path.exists(self.job_file):
                with open(self.job_file, 'r', encoding='utf-8') as f:
                    jobs = json.load(f)
                
                for job in jobs:
                    self.job_queue.append(job)
                
                self.logger.info(f"Carregados {len(jobs)} trabalhos pendentes")
            else:
                self.logger.info("Nenhum arquivo de trabalhos pendentes encontrado")
        except Exception as e:
            self.logger.error(f"Erro ao carregar trabalhos pendentes: {e}")
    
    def save_pending_jobs(self):
        """Salva trabalhos pendentes no arquivo de persistência"""
        try:
            jobs = list(self.job_queue)
            with open(self.job_file, 'w', encoding='utf-8') as f:
                json.dump(jobs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"Erro ao salvar trabalhos pendentes: {e}")
    
    def add_job_to_queue(self, product_id):
        """Adiciona um trabalho à fila e persiste"""
        job = {
            'product_id': product_id,
            'timestamp': datetime.now().isoformat(),
            'attempts': 0
        }
        
        self.job_queue.append(job)
        self.save_pending_jobs()
        self.logger.info(f"Produto '{product_id}' adicionado à fila")
    
    def validate_product_id(self, product_id):
        """Valida o formato do product_id"""
        if not product_id or not isinstance(product_id, str):
            return False
        
        # Remove espaços em branco
        product_id = product_id.strip()
        
        # Verifica se é numérico ou alfanumérico válido
        if re.match(r'^[a-zA-Z0-9-_]+$', product_id):
            return True
        
        return False
    
    def process_job(self, job):
        """Processa um trabalho da fila"""
        product_id = job['product_id']
        
        if not self.validate_product_id(product_id):
            self.logger.error(f"Product ID inválido: {product_id}")
            return False
        
        connection = self.get_db_connection()
        if not connection:
            self.logger.error("Não foi possível obter conexão com o banco")
            return False
        
        try:
            cursor = connection.cursor(dictionary=True)
            
            # Inicia transação
            connection.start_transaction()
            
            # Verifica se o produto existe em qualquer loja/restaurante configurado.
            store_placeholders = ', '.join(['%s'] * len(self.store_keys))
            check_query = (
                f"SELECT * FROM tb_produto "
                f"WHERE id_produto = %s AND store_key IN ({store_placeholders}) "
                f"ORDER BY FIELD(store_key, {store_placeholders}) "
                f"LIMIT 1 FOR UPDATE"
            )
            cursor.execute(check_query, [product_id] + self.store_keys + self.store_keys)
            produto = cursor.fetchone()
            
            if not produto:
                self.logger.warning(
                    "Produto não encontrado: ID=%s, Stores=%s",
                    product_id,
                    ', '.join(self.store_keys)
                )
                connection.rollback()
                return False
            
            if Decimal(str(produto['quantidade'])) < Decimal('1') or produto['status'] != 'ativo':
                self.logger.warning("Produto sem unidade disponivel: %s", product_id)
                connection.rollback()
                return False

            # Mantem o saldo por armazem consistente com a baixa do lote.
            cursor.execute(
                "SELECT id, quantity FROM warehouse_stocks "
                "WHERE lot_id = %s AND store_key = %s "
                "ORDER BY (warehouse_id = %s) DESC, id FOR UPDATE",
                (product_id, produto['store_key'], produto.get('print_warehouse_id') or 0)
            )
            warehouse_rows = cursor.fetchall()
            if warehouse_rows:
                available = sum((Decimal(str(row['quantity'])) for row in warehouse_rows), Decimal('0'))
                if available < Decimal('1'):
                    self.logger.error("Saldo de armazem insuficiente para o lote %s", product_id)
                    connection.rollback()
                    return False
                to_remove = Decimal('1')
                for row in warehouse_rows:
                    debit = min(max(Decimal(str(row['quantity'])), Decimal('0')), to_remove)
                    if debit:
                        cursor.execute(
                            "UPDATE warehouse_stocks SET quantity = quantity - %s, updated_at = NOW() "
                            "WHERE id = %s AND store_key = %s",
                            (debit, row['id'], produto['store_key'])
                        )
                        to_remove -= debit
                    if not to_remove:
                        break

            # Prepara dados para tb_produto_removido com horário de São Paulo
            from datetime import timezone, timedelta
            sao_paulo_tz = timezone(timedelta(hours=-3))
            data_retirada_sp = datetime.now(sao_paulo_tz)
            
            # Monta dados para inserção na tabela tb_produto_removido
            # Não incluímos id_removido pois será gerado automaticamente (AUTO_INCREMENT)
            unit_quantity = Decimal('1')
            produto_removido = {
                'store_key': produto['store_key'],
                'nome': produto['nome'],
                'peso': produto['peso'],
                'grupo': produto['grupo'],
                'quantidade': unit_quantity,
                'conservacao': produto['conservacao'],
                'data_entrada': produto['data_entrada'],
                'data_retirada': data_retirada_sp,
                'validade': produto['validade'],
                'responsavel_entrada': produto['responsavel_entrada'],
                'responsavel_retirada': 'Leitor QRCODE',
                'preco': produto['preco'],
                'unidade_medida': produto['unidade_medida'],
                'etiquetas': unit_quantity,
                'status': produto['status'],
                'fornecedor': produto['fornecedor'],
                'sif_lote': produto['sif_lote'],
                'val_fornecedor': produto['val_fornecedor'],
                'fab_fornecedor': produto['fab_fornecedor'],
                'status_impressao': produto['status_impressao']
            }
            
            # Insere na tabela tb_produto_removido
            columns = ', '.join(produto_removido.keys())
            placeholders = ', '.join(['%s'] * len(produto_removido))
            insert_query = f"INSERT INTO tb_produto_removido ({columns}) VALUES ({placeholders})"
            
            cursor.execute(insert_query, list(produto_removido.values()))
            
            remaining_quantity = Decimal(str(produto['quantidade'])) - unit_quantity
            remaining_labels = Decimal(str(produto.get('etiquetas') or 0)) - unit_quantity

            if remaining_quantity > 0:
                update_query = (
                    "UPDATE tb_produto "
                    "SET quantidade = %s, etiquetas = %s "
                    "WHERE id_produto = %s AND store_key = %s"
                )
                cursor.execute(
                    update_query,
                    (
                        remaining_quantity,
                        max(remaining_labels, Decimal('0')),
                        product_id,
                        produto['store_key']
                    )
                )
            elif warehouse_rows:
                # Preserva os vinculos e o historico de lotes esgotados.
                cursor.execute(
                    "UPDATE tb_produto SET quantidade = 0, etiquetas = 0, status = 'inativo' "
                    "WHERE id_produto = %s AND store_key = %s",
                    (product_id, produto['store_key'])
                )
            else:
                # Remove da tabela original quando a última unidade é baixada.
                delete_query = "DELETE FROM tb_produto WHERE id_produto = %s AND store_key = %s"
                cursor.execute(delete_query, (product_id, produto['store_key']))
            
            # Confirma transação
            connection.commit()
            
            self.logger.info(
                "Produto processado com sucesso: %s (Store=%s, unidade removida=1, quantidade restante=%s)",
                product_id,
                produto['store_key'],
                max(remaining_quantity, Decimal('0'))
            )
            return True
            
        except MySQLError as e:
            self.logger.error(f"Erro MySQL ao processar produto {product_id}: {e}")
            connection.rollback()
            return False
        except Exception as e:
            self.logger.error(f"Erro geral ao processar produto {product_id}: {e}")
            connection.rollback()
            return False
        finally:
            cursor.close()
            connection.close()
    
    def queue_processor(self):
        """Thread para processar a fila de trabalhos"""
        self.logger.info("Thread de processamento da fila iniciada")
        while self.running:
            try:
                # Log de debug para monitorar o estado
                if len(self.job_queue) > 0:
                    self.logger.debug(f"Fila tem {len(self.job_queue)} itens. DB conectado: {self.db_connected}")
                
                if self.job_queue and self.db_connected:
                    job = self.job_queue.popleft()
                    self.logger.info(f"Processando job: {job['product_id']}")
                    
                    if self.process_job(job):
                        # Sucesso - remove do arquivo
                        self.save_pending_jobs()
                    else:
                        # Falha - recoloca na fila com incremento de tentativas
                        job['attempts'] += 1
                        if job['attempts'] < 3:
                            self.job_queue.append(job)
                            self.save_pending_jobs()
                            self.logger.warning(f"Recolocando job na fila. Tentativa {job['attempts']}/3")
                        else:
                            self.logger.error(f"Job descartado após 3 tentativas: {job['product_id']}")
                elif self.job_queue and not self.db_connected:
                    self.logger.warning(f"Fila tem {len(self.job_queue)} itens mas banco não está conectado")
                
                time.sleep(1)  # Evita uso excessivo de CPU
                
            except Exception as e:
                self.logger.error(f"Erro no processador de fila: {e}")
                time.sleep(5)
    
    def db_reconnect_worker(self):
        """Thread para reconexão com backoff exponencial"""
        backoff_time = 1
        max_backoff = 300  # 5 minutos
        
        while self.running:
            if not self.db_connected:
                self.logger.info(f"Tentando reconectar ao banco. Aguardando {backoff_time}s...")
                time.sleep(backoff_time)
                
                if self.setup_database_pool():
                    self.logger.info("Reconexão com banco bem-sucedida")
                    backoff_time = 1  # Reset backoff
                else:
                    backoff_time = min(backoff_time * 2, max_backoff)
            else:
                time.sleep(10)  # Verifica a cada 10 segundos
    
    def find_qr_device(self):
        """Encontra o dispositivo do leitor QR Code com detecção inteligente"""
        if not EVDEV_AVAILABLE:
            self.logger.error("evdev não está disponível. Instale com: pip install evdev")
            return None
        
        try:
            devices = []
            for path in evdev.list_devices():
                try:
                    device = evdev.InputDevice(path)
                    devices.append(device)
                except (OSError, PermissionError) as e:
                    self.logger.debug(f"Não foi possível acessar dispositivo {path}: {e}")
                    continue
            
            if not devices:
                self.logger.debug("Nenhum dispositivo de entrada encontrado")
                return None
            
            # Lista de critérios de prioridade para identificar leitores QR Code
            qr_criteria = [
                # Prioridade 1: Dispositivos conhecidos de QR Code
                ['arm cm0', 'cm0'],
                # Prioridade 2: Termos específicos de leitores
                ['barcode', 'scanner', 'qr', 'code reader', 'honeywell', 'datalogic', 'symbol'],
                # Prioridade 3: Dispositivos USB HID genéricos que podem ser leitores
                ['usb hid', 'hid keyboard'],
                # Prioridade 4: Teclados USB (fallback)
                ['usb keyboard', 'keyboard']
            ]
            
            self.logger.debug(f"Dispositivos de entrada encontrados: {len(devices)}")
            for device in devices:
                self.logger.debug(f"  - {device.name} ({device.path})")

            # Permite fixar explicitamente um device path por ambiente.
            if self.force_device_path:
                forced = next((d for d in devices if d.path == self.force_device_path), None)
                if forced and self.validate_device_capabilities(forced):
                    self.logger.info(f"Usando dispositivo forçado via LEITOR_DEVICE_PATH: {forced.name} ({forced.path})")
                    return forced
                self.logger.warning(f"LEITOR_DEVICE_PATH configurado, mas inválido/inacessível: {self.force_device_path}")
            
            # Primeiro, tenta usar o dispositivo preferido se disponível
            if self.preferred_device:
                for device in devices:
                    if (device.name == self.preferred_device['name'] and 
                        device.path == self.preferred_device['path']):
                        if self.validate_device_capabilities(device):
                            # Testa a funcionalidade do dispositivo preferido
                            if self.test_device_functionality(device, timeout=2):
                                self.logger.info(f"Usando dispositivo preferido validado: {device.name} ({device.path})")
                                return device
                            else:
                                self.logger.warning(f"Dispositivo preferido {device.name} não passou no teste de funcionalidade")
                                # Limpa o dispositivo preferido se não for mais funcional
                                self.preferred_device = None
                                break
                        else:
                            self.logger.warning(f"Dispositivo preferido {device.name} não tem capacidades adequadas")
                            break
            
            # Procura por dispositivos seguindo a ordem de prioridade
            for priority, keywords in enumerate(qr_criteria, 1):
                for device in devices:
                    device_name = device.name.lower()
                    
                    # Verifica se alguma palavra-chave corresponde
                    for keyword in keywords:
                        if keyword in device_name:
                            # Valida se o dispositivo tem capacidades de teclado
                            if self.validate_device_capabilities(device):
                                # Testa a funcionalidade do dispositivo
                                if self.test_device_functionality(device, timeout=2):
                                    self.logger.info(f"Dispositivo selecionado e validado (prioridade {priority}): {device.name} ({device.path})")
                                    # Salva como dispositivo preferido para próximas execuções
                                    self.save_device_config(device)
                                    return device
                                else:
                                    self.logger.warning(f"Dispositivo {device.name} não passou no teste de funcionalidade")
                                    break
                            else:
                                self.logger.warning(f"Dispositivo {device.name} não tem capacidades adequadas")
                                break
            
            self.logger.debug("Nenhum dispositivo de entrada adequado encontrado")
            return None
            
        except Exception as e:
            self.logger.error(f"Erro ao procurar dispositivos: {e}")
            return None
    
    def validate_device_capabilities(self, device):
        """Valida se o dispositivo tem capacidades de entrada de teclado"""
        try:
            capabilities = device.capabilities()
            
            # Verifica se o dispositivo suporta eventos de teclado
            if ecodes.EV_KEY not in capabilities:
                return False
            
            # Verifica se tem ao menos tecla de finalização.
            key_codes = capabilities[ecodes.EV_KEY]
            has_enter = (ecodes.KEY_ENTER in key_codes) or (getattr(ecodes, 'KEY_KPENTER', -1) in key_codes)
            return has_enter
            
        except Exception as e:
            self.logger.warning(f"Erro ao validar capacidades do dispositivo {device.name}: {e}")
            return False
    
    def check_for_new_devices(self):
        """Verifica se novos dispositivos foram conectados"""
        current_time = time.time()
        
        # Só verifica se passou o intervalo definido
        if current_time - self.last_device_scan < self.device_scan_interval:
            return False
        
        self.last_device_scan = current_time
        
        try:
            if not EVDEV_AVAILABLE:
                return False
            
            # Se já temos um dispositivo funcionando, não precisa procurar
            if self.input_device:
                try:
                    self.input_device.capabilities()
                    return False  # Dispositivo atual ainda funciona
                except (OSError, IOError):
                    self.logger.info("Dispositivo atual não está mais acessível. Procurando novos dispositivos...")
                    self.input_device = None
            
            # Procura por novos dispositivos
            new_device = self.find_qr_device()
            if new_device:
                self.logger.info(f"Novo dispositivo detectado: {new_device.name} ({new_device.path})")
                return True
            
            return False
            
        except Exception as e:
            self.logger.warning(f"Erro ao verificar novos dispositivos: {e}")
            return False
    
    def test_device_functionality(self, device, timeout=5):
        """Testa se o dispositivo está funcionando corretamente"""
        try:
            if not device:
                return False
            
            # Verifica se o dispositivo ainda está acessível
            try:
                device.capabilities()
            except (OSError, IOError):
                self.logger.warning(f"Dispositivo {device.name} não está mais acessível")
                return False
            
            # Testa se consegue ler eventos (com timeout)
            import select
            
            # Configura o dispositivo para não bloquear
            device.grab()
            
            try:
                # Usa select para verificar se há dados disponíveis
                ready, _, _ = select.select([device.fd], [], [], timeout)
                
                if ready:
                    # Há dados disponíveis, tenta ler um evento
                    try:
                        events = list(device.read())
                        self.logger.debug(f"Dispositivo {device.name} respondeu com {len(events)} eventos")
                        return True
                    except Exception:
                        pass
                
                # Se não há eventos, ainda considera funcional
                # (dispositivo pode estar inativo mas funcionando)
                self.logger.debug(f"Dispositivo {device.name} está acessível mas sem eventos no momento")
                return True
                
            finally:
                try:
                    device.ungrab()
                except:
                    pass
            
        except Exception as e:
            self.logger.warning(f"Erro ao testar funcionalidade do dispositivo: {e}")
            return False
        
    def log_hardware_info(self):
        """Registra informações detalhadas sobre o hardware para troubleshooting"""
        try:
            self.logger.info("=== INFORMAÇÕES DE HARDWARE ===")
            
            # Informações do sistema
            import platform
            self.logger.info(f"Sistema: {platform.system()} {platform.release()}")
            self.logger.info(f"Arquitetura: {platform.machine()}")
            
            if not EVDEV_AVAILABLE:
                self.logger.warning("Biblioteca evdev não está disponível")
                return
            
            # Lista todos os dispositivos de entrada
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
            self.logger.info(f"Total de dispositivos de entrada encontrados: {len(devices)}")
            
            for i, device in enumerate(devices, 1):
                try:
                    caps = device.capabilities()
                    has_keyboard = evdev.ecodes.EV_KEY in caps
                    
                    self.logger.info(f"Dispositivo {i}: {device.name}")
                    self.logger.info(f"  Caminho: {device.path}")
                    self.logger.info(f"  Vendor ID: {device.info.vendor:04x}")
                    self.logger.info(f"  Product ID: {device.info.product:04x}")
                    self.logger.info(f"  Versão: {device.info.version}")
                    self.logger.info(f"  Suporte a teclado: {'Sim' if has_keyboard else 'Não'}")
                    
                    if has_keyboard:
                        # Verifica teclas específicas importantes
                        key_caps = caps.get(evdev.ecodes.EV_KEY, [])
                        has_enter = evdev.ecodes.KEY_ENTER in key_caps
                        has_numbers = any(getattr(evdev.ecodes, f'KEY_{i}', None) in key_caps for i in range(10))
                        
                        self.logger.info(f"  Tecla ENTER: {'Sim' if has_enter else 'Não'}")
                        self.logger.info(f"  Teclas numéricas: {'Sim' if has_numbers else 'Não'}")
                    
                    self.logger.info("  ---")
                    
                except Exception as e:
                    self.logger.warning(f"Erro ao obter informações do dispositivo {device.path}: {e}")
            
            # Informações sobre dispositivos USB
            try:
                import subprocess
                result = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    self.logger.info("=== DISPOSITIVOS USB ===")
                    for line in result.stdout.strip().split('\n'):
                        if line.strip():
                            self.logger.info(f"USB: {line}")
            except Exception as e:
                self.logger.debug(f"Não foi possível obter informações USB: {e}")
            
            self.logger.info("=== FIM DAS INFORMAÇÕES DE HARDWARE ===")
            
        except Exception as e:
            self.logger.error(f"Erro ao registrar informações de hardware: {e}")
    
    def monitor_input_events(self):
        """Monitora eventos de entrada do leitor QR Code com fallback automático"""
        device_retry_count = 0
        missing_device_alerted = False
        # Intervalos de espera progressivos: 10s, 30s, 60s (máximo)
        retry_intervals = [10, 30, 60]

        while self.running:
            try:
                if not self.input_device:
                    self.input_device = self.find_qr_device()

                    if not self.input_device:
                        wait = retry_intervals[min(device_retry_count, len(retry_intervals) - 1)]
                        if not missing_device_alerted:
                            self.logger.warning("Nenhum leitor QR conectado. Aguardando dispositivo.")
                            missing_device_alerted = True
                        device_retry_count += 1
                        time.sleep(wait)
                        continue
                    else:
                        device_retry_count = 0
                        missing_device_alerted = False
                        self.logger.info(f"Leitor conectado: {self.input_device.name} ({self.input_device.path})")
                
                # Testa se o dispositivo ainda está acessível
                try:
                    self.input_device.capabilities()
                except (OSError, IOError):
                    self.logger.warning("Dispositivo não está mais acessível. Procurando novo dispositivo...")
                    self.input_device = None
                    continue
                
                # Verifica periodicamente se há novos dispositivos (reconexão automática)
                self.check_for_new_devices()
                
                # Monitora eventos
                for event in self.input_device.read_loop():
                    if not self.running:
                        break
                    
                    if event.type == ecodes.EV_KEY:
                        key_event = categorize(event)
                        
                        if key_event.keystate == key_event.key_down:
                            if key_event.keycode == 'KEY_ENTER':
                                # Fim da leitura do QR Code
                                if self.current_input.strip():
                                    product_id = self.current_input.strip()
                                    self.logger.info(f"QR Code lido: {product_id}")
                                    
                                    if self.validate_product_id(product_id):
                                        self.add_job_to_queue(product_id)
                                    else:
                                        self.logger.error(f"QR Code inválido: {product_id}")
                                
                                self.current_input = ""
                            
                            elif hasattr(key_event, 'keycode') and key_event.keycode.startswith('KEY_'):
                                # Mapeia teclas para caracteres
                                char = self.keycode_to_char(key_event.keycode)
                                if char:
                                    self.current_input += char
            
            except OSError as e:
                self.logger.error(f"Dispositivo de entrada desconectado: {e}")
                self.input_device = None
                # Limpa dispositivo preferido se ele falhou
                if self.preferred_device and self.input_device and self.input_device.name == self.preferred_device['name']:
                    self.logger.warning("Dispositivo preferido falhou. Será procurado um novo dispositivo.")
                time.sleep(5)
            except Exception as e:
                self.logger.error(f"Erro no monitoramento de entrada: {e}")
                time.sleep(5)
    
    def keycode_to_char(self, keycode):
        """Converte keycode para caractere"""
        key_map = {
            'KEY_0': '0', 'KEY_1': '1', 'KEY_2': '2', 'KEY_3': '3', 'KEY_4': '4',
            'KEY_5': '5', 'KEY_6': '6', 'KEY_7': '7', 'KEY_8': '8', 'KEY_9': '9',
            'KEY_A': 'a', 'KEY_B': 'b', 'KEY_C': 'c', 'KEY_D': 'd', 'KEY_E': 'e',
            'KEY_F': 'f', 'KEY_G': 'g', 'KEY_H': 'h', 'KEY_I': 'i', 'KEY_J': 'j',
            'KEY_K': 'k', 'KEY_L': 'l', 'KEY_M': 'm', 'KEY_N': 'n', 'KEY_O': 'o',
            'KEY_P': 'p', 'KEY_Q': 'q', 'KEY_R': 'r', 'KEY_S': 's', 'KEY_T': 't',
            'KEY_U': 'u', 'KEY_V': 'v', 'KEY_W': 'w', 'KEY_X': 'x', 'KEY_Y': 'y',
            'KEY_Z': 'z', 'KEY_MINUS': '-', 'KEY_EQUAL': '='
        }
        return key_map.get(keycode, '')
    
    def signal_handler(self, signum, frame):
        """Manipula sinais para encerramento gracioso"""
        self.logger.info(f"Sinal {signum} recebido. Encerrando serviço...")
        self.stop()
    
    def start(self):
        """Inicia o serviço"""
        self.logger.info("Iniciando serviço Stockflow QR")
        
        # Carrega configuração
        if not self.load_configuration():
            self.logger.error("Falha ao carregar configuração. Encerrando.")
            return False
        
        # Carrega trabalhos pendentes
        self.load_pending_jobs()
        
        # Configura banco de dados
        if not self.setup_database_pool():
            self.logger.warning("Falha inicial na conexão com banco. Continuando...")
        
        self.running = True
        
        # Inicia threads
        self.queue_processor_thread = threading.Thread(target=self.queue_processor, daemon=True)
        self.queue_processor_thread.start()
        
        self.db_reconnect_thread = threading.Thread(target=self.db_reconnect_worker, daemon=True)
        self.db_reconnect_thread.start()
        
        # Inicia monitoramento de entrada (thread principal)
        self.logger.info("Serviço iniciado. Aguardando leituras de QR Code...")
        self.monitor_input_events()
        
        return True
    
    def stop(self):
        """Para o serviço"""
        self.running = False
        
        # Salva trabalhos pendentes
        self.save_pending_jobs()
        
        self.logger.info("Serviço encerrado")

def main():
    """Função principal"""
    service = StockflowQRService()
    
    try:
        service.start()
    except KeyboardInterrupt:
        service.logger.info("Interrupção pelo usuário")
    except Exception as e:
        service.logger.error(f"Erro fatal: {e}")
    finally:
        service.stop()

if __name__ == "__main__":
    main()
