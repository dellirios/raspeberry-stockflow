# Validação em Raspberry Pi 1 B+ — 09/11/2026

Instalação em Raspberry Pi 1 Model B+ V1.2, ARMv6 de 32 bits, Raspbian 13 e Python 3.13.5. Diretório adotado: `/home/stockflow/stockflow`. Agente executado como usuário comum, serviço `stockflow-python.service` habilitado no boot, CUPS 2.4.10 e fila RAW USB. HTTP local restrito a `127.0.0.1:2508`.

## Resultados observados

- 31 testes passaram no Python 3.13.5/ARMv6 (262,131 segundos com instalação de pacotes concorrente).
- Credencial da instalação aceita pela API e heartbeat confirmado.
- Consulta USB de status e contador da Zebra ZD220 funcionando.
- Uma etiqueta de teste enviada uma única vez: CUPS concluído, incremento exato de uma unidade no contador e saída física confirmada pelo operador. Renderização do exemplo: 0,798 segundo.
- Pedido real de uma unidade concluído, com evento final confirmado pelo servidor. Repetição na consulta bloqueada pela deduplicação.
- Tampa aberta detectada: mesmo pedido aguardou, retomou após correção e concluiu com um único envio. Servidor confirmou o evento final.
- Calibração pelo painel executada, conclusão física confirmada pelo operador e retorno aceito pelo servidor.
- Sem falhas ou conferências pendentes ao final das verificações acima.

## Instalação e operação

A regra udev deve conter o serial real da impressora. Configure o grupo `lp` e as permissões do dispositivo USB; não execute o agente como root. `.env` e `config/printers.json` devem permanecer privados (modo 0600). Use credencial própria de cada instalação. Ao substituir um agente, pare o antigo antes de ativar o novo sobre a mesma loja/impressora.

```bash
systemctl status stockflow-python
journalctl -u stockflow-python -n 50 --no-pager
curl http://127.0.0.1:2508/health
sudo systemctl stop stockflow-python
```

Não apague journals para liberar pedidos. Configurações, logs, identificadores de pedidos, seriais e credenciais da instalação real não fazem parte deste repositório.

## Pendências e limites

- Falta de etiqueta e retomada foram informadas pelo operador como aprovadas; a sequência detalhada desse teste não foi auditada separadamente neste registro.
- Fim real de ribbon, desconexão USB, reinício durante envio e operação prolongada ainda precisam de validação física nesta versão Python.
- Leitor QR opcional ainda não instalado/ativado no Raspberry de validação.
- Alinhamento e leitura do QR precisam ser conferidos fisicamente ao trocar impressora/mídia.
- Houve registro histórico de subtensão no equipamento, sem subtensão ativa na leitura da instalação; acompanhar alimentação em operação.
- O CUPS instalado aceita fila RAW, mas avisa que esse recurso será removido em uma versão futura.
- Windows e confirmação física em outros modelos ainda não homologados.
