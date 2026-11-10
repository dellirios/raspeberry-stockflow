# StockFlow — agente Python para Raspberry Pi

Portabilidade local do agente Node 2.1.0 para **Raspberry Pi 1 Model B+ Rev. 1.2, ARMv6 de 32 bits**. Alvo: Raspberry Pi OS Lite / Raspbian 13, Python 3.13.5. Requer Python >=3.11.

Estado: instalado no Raspberry em `/home/stockflow/stockflow`, com serviço de impressão ativo no boot. **31 testes passaram no ARMv6 e uma etiqueta física foi confirmada por contador e CUPS.** Veja [registro da instalação](docs/instalacao-raspberry.md) para validações e pendências. Versão de desenvolvimento `2.1.0.dev1`; não representa uma nova release publicada no GitHub.

O agente de impressão usa somente a biblioteca padrão do Python. Não requer Node.js, navegador, interface gráfica ou Docker. O leitor QR já existente foi reaproveitado e continua separado, com dependências opcionais.

## O que foi portado

- Layout Zebra aprovado de 60 × 60 mm / 480 × 480 dots em ZPL nativo, incluindo os oito cards com contornos de 3 dots, cabeçalhos invertidos, espaçamentos e QR ampliado.
- Layout Elgin existente e configurações de geometria/escuridão, sem redesenho.
- Fila por loja, quantidade, identificação do pedido e filtro de lojas autorizadas.
- Confirmação física Zebra por `~HS`, contador e estado final do trabalho CUPS; duas leituras ociosas consecutivas.
- Estados de tampa, pausa, etiqueta, ribbon e comunicação; eventos persistentes enviados ao servidor com confirmação de identidade.
- Proteção contra reimpressão de envio incerto, recuperação de reinício e conferência manual.
- Calibração solicitada pelo servidor, inclusive quando um pedido está bloqueado antes do envio.
- Heartbeat independente a cada 10 segundos, status da impressora e polling configurável em 1 segundo com variação de ±30%.
- `/health`, `/metrics`, rota local de consulta/impressão e prévia ZPL sem impressão.
- Leitor QR original: mesmas consultas e regras de baixa, caminhos relativos e pool MySQL reduzido de 5 para 1 conexão.

## Testar localmente, sem impressora e sem servidor

Execute na raiz desta pasta:

```powershell
python -m unittest discover -s tests -v
python -m stockflow check
python -m stockflow preview
```

`check` não abre USB nem conecta à API. `preview` usa `examples/label.json`, um exemplo fictício, e grava `logs/preview/etiqueta-enviada.zpl` e `status.json`. Não imprime e não transmite dados a renderizadores externos.

```powershell
python -m stockflow preview --input meu-exemplo.json --output minha-etiqueta.zpl
python -m stockflow preview --kind elgin --input examples/elgin-label.json
```

O arquivo indicado em `--input` deve conter os campos de uma etiqueta, sem o envelope `data`. A prévia é ZPL; não há renderizador PNG/PDF novo nesta versão. Os testes comparam a saída Zebra/Elgin com oito resultados sintéticos gerados pelos renderizadores Node originais, sem chamar o serviço de impressão.

## Obter o código

```bash
git clone https://github.com/dellirios/raspeberry-stockflow.git /home/stockflow/stockflow
cd /home/stockflow/stockflow
```

## Preparação de uma nova instalação no Raspberry

Estes comandos orientam novas instalações; o registro acima descreve o que foi executado nesta máquina. Copie esta pasta completa para `/home/stockflow/stockflow`; execute diretamente dessa pasta. Não instale apenas o pacote `stockflow`, porque templates, helper USB e leitor também fazem parte da instalação.

```bash
sudo apt update
sudo apt install python3 python3-venv cups cups-client cups-ipp-utils libusb-1.0-0
cd /home/stockflow/stockflow
cp .env.example .env
cp config/printers.json.example config/printers.json
chmod 600 .env config/printers.json
```

Preencha a loja, o nome exato da fila CUPS, o serial e uma credencial própria do dispositivo. A credencial precisa existir no sistema principal; o agente não cria acesso, não altera APIs e não cadastra outro dispositivo automaticamente. Não execute dois agentes sobre a mesma fila/loja.

Consulte a URI real com `lpinfo -v`; crie uma fila RAW usando essa URI. O nome deve corresponder a `nome` no JSON e `ZEBRA_PRINTER_NAME` no ambiente. RAW é necessário para não converter ZPL. Disponibilidade da fila RAW e funcionamento do backend USB precisam ser verificados no CUPS instalado.

Adapte o serial em `deploy/70-stockflow-zebra.rules.example`, instale a regra e recarregue o udev com a impressora ociosa. O usuário do serviço precisa ter acesso USB e à fila; a regra usa o grupo `stockflow`. Não execute o agente como root para contornar permissões.

```bash
python3 -m stockflow check
# Somente quando a loja e a fila estiverem corretas: este comando consulta pedidos e pode imprimir.
python3 -m stockflow run
```

Após o teste físico de uma unidade, revise os caminhos/usuário do arquivo `deploy/stockflow-python.service` e instale-o em `/etc/systemd/system/`. O daemon carrega `.env` da raiz. Não é necessário instalar bibliotecas Python para imprimir.

### Leitor QR opcional

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-reader.txt
cp leitor/leitor.env.example leitor/leitor.env
chmod 600 leitor/leitor.env
# Preencha a conexão de banco; este comando pode efetuar baixas reais.
.venv/bin/python -m stockflow reader
```

Em ARMv6, prefira pacotes de distribuição quando uma dependência precisar compilar. `evdev` pode exigir `python3-dev`, `gcc` e cabeçalhos Linux; as dependências ainda não foram instaladas/testadas nesse Raspberry. O conector é configurado com `use_pure=True`. Para serviço, revise `deploy/stockflow-reader.service`; o leitor precisa do grupo `input` e permissão de leitura dos dispositivos corretos.

O banco não teve esquema ou regras alterados. A limitação já existente entre commit da baixa e retirada do trabalho da fila do leitor permanece: um desligamento nesse intervalo pode repetir uma baixa. Não homologar recuperação de falhas do leitor sem testar esse cenário separadamente.

## Comunicação e arquivos

Veja [protocolo e recuperação](docs/protocolo-e-recuperacao.md) e [validação da portabilidade](docs/validacao.md).

| Arquivo/módulo | Responsabilidade |
|---|---|
| `stockflow/layout.py` | ZPL Zebra/Elgin, medidas e configurações físicas |
| `stockflow/service.py` | Lojas, lotes, deduplicação e integração |
| `stockflow/feedback.py` | Evidência física e journal de impressão |
| `stockflow/protocol.py` | HTTPS, eventos persistentes e calibração |
| `stockflow/runtime.py` | Polling, heartbeat, HTTP local e exclusão de instâncias |
| `stockflow/linux.py` | Fronteira de hardware CUPS/libusb |
| `scripts/zebra-usb-feedback.py` | Helper USB Python reaproveitado |
| `leitor/leitor.py` | Leitor QR existente, sem nova regra de negócio |
| `config/printers.json` | Configuração privada de loja/fila |
| `.env`, `leitor/leitor.env` | Credenciais privadas, nunca versionar |
| `logs/print-feedback/*.json` | Intenção de envio, observação recente, estados e confirmação de entrega |
| `logs/printer-calibration/*.json` | Comandos e resultados de calibração |
| `logs/leitor/` | Fila e log do leitor QR |

`stockflow` original e `windows-stockflow` não foram modificados. A futura versão Windows reutilizará o núcleo, mas ainda precisa de transporte de impressão/status e captura do leitor próprios. Não existe suporte físico Windows nesta entrega.
