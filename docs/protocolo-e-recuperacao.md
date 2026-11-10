# Protocolo e recuperação

Esta portabilidade trabalha com os endpoints que já existem no sistema principal. Não altera servidor, schema, autenticação, permissões ou cadastros.

## Modo com confirmação física

Habilite `ZEBRA_FEEDBACK_LOCAL_ONLY=true`; o nome da fila deve ser exatamente `ZEBRA_PRINTER_NAME`. Configure serial e credencial próprios do equipamento. Mesmo com nome semelhante, outra fila não herda confirmação física. Com token moderno, pedidos de filas sem feedback são bloqueados, em vez de enviados como legado.

- `GET https://stockflow.pro/start.php?store_key=...&print_feedback=1&calibration_capable=1`, Bearer do dispositivo.
- `POST https://stockflow.pro/api/print-agent/events`, Bearer; resposta exige `accepted: true` e `event_id` idêntico.
- `GET https://stockflow.pro/api/print-agent/commands`, durante espera antes do envio, no máximo a cada 2 segundos.
- `POST https://stockflow.pro/api/print-agent/commands/UUID`, com `state` e `message`; resposta exige `accepted: true` e `command_id` idêntico.
- `POST https://stockflow.pro/api/print-agent/heartbeat`, a cada 10 segundos, com `printer: {state, reasons, age_seconds}`; exige `accepted: true`.

Conexões HTTPS são reutilizadas por thread. Não há redirecionamento automático ou reenvio automático de POST pelo cliente HTTP. Somente o domínio oficial é permitido. Eventos têm nova tentativa com os mesmos IDs; o servidor precisa continuar tratando duplicatas de forma idempotente, como no agente Node.

Payload de evento:

```json
{
  "event_id": "UUID:1", "attempt_id": "UUID", "sequence": 1,
  "state": "recebido", "reasons": [], "printed": 0, "expected": 1,
  "at": "2026-11-09T00:00:00.000Z", "source": "agent",
  "printer_serial": "SERIAL_CONFIGURADO", "printer_name": "FILA_CONFIGURADA",
  "cups_job_id": null,
  "items": [{"id_produto":"EXEMPLO", "store_key":"SUA_STORE_KEY",
    "print_request_id":"UUID", "print_batch_uuid":null, "label_code":null}]
}
```

O ID do produto e os campos da etiqueta continuam vindo da API. O QR codifica o `id_produto` original, normalizado em NFC, sem trocar o valor pelo UUID da tentativa.

## Evidência física

O fluxo aguarda impressora ociosa e fila sem trabalho pendente, captura o contador inicial, persiste a intenção e só então chama `lp -o raw` uma vez. O journal recebe o ID retornado pelo CUPS. Para concluir exige:

1. Trabalho CUPS em `completed`.
2. Incremento exato do contador igual à soma das quantidades enviadas.
3. Duas observações consecutivas sem bloqueios, formatos pendentes, formato parcial ou etiquetas restantes.

Tampa aberta, falta de etiqueta, ribbon, pausa e temperatura mantêm o mesmo pedido aguardando enquanto há comunicação válida. Não existe reenvio ou comando de resume/calibração automático. O usuário pode precisar apertar Start/Pause após corrigir uma condição física.

Perda de comunicação por 120 segundos, contador reiniciado/excessivo, interferência de outro trabalho, cancelamento CUPS ou reinício após intenção de envio deixam `confirmacao_pendente`. Nunca confirmar apenas porque `lp` aceitou dados. O contador é global: outra aplicação imprimindo na mesma Zebra invalida essa evidência. A confirmação não comprova contraste, conteúdo visual ou leitura física do QR.

## Persistência

`logs/print-feedback/UUID.json` guarda estados, sequência de eventos, confirmação de entrega e última observação. Gravação com arquivo temporário privado, `fsync`, substituição atômica e sincronização do diretório no Linux. Nesta portabilidade a observação mais recente é preservada; não há um segundo arquivo crescente de observações brutas a cada 500 ms.

A falta de confirmação da API repete somente eventos; não repete etiquetas. Não apague journals para “destravar” pedidos. Faça backup antes de manutenção. Histórico e eventos concluídos ainda não têm expurgo automático: acompanhe tamanho de `logs` e consumo de memória em operação prolongada.

Uma trava de processo impede iniciar duas cópias nesta mesma instalação. Ela não impede outro programa ou uma instalação diferente de usar a mesma impressora.

### Conferência manual

Pare o serviço antes de executar. A CLI também exige a trava de instância livre, impressora ociosa, fila vazia e trabalho original terminal. Um envio sem ID CUPS conhecido precisa de reconciliação antes de liberar pelo utilitário.

```bash
python3 -m stockflow review list
python3 -m stockflow review confirm UUID --reason "Conferi fisicamente a etiqueta produzida"
python3 -m stockflow review release UUID --reason "Conferi a fila e confirmei que nao houve impressao"
```

`confirm` registra conclusão manual e quantidade esperada; `release` registra cancelamento/liberação local. Não envia ZPL, não cancela fila nem apaga buffers. Ao reiniciar o agente, os novos eventos serão entregues; a semântica de reenvio continua sob controle da fila do servidor.

## Calibração

Somente comandos `calibrate` com UUID válido são aceitos. A calibração disputa a mesma trava da impressora. Um pedido bloqueado **antes** do envio pode ser suspenso para processar a calibração e retomado depois com a mesma tentativa.

Não interrompe trabalho já submetido ou aguardando conferência. Permite pré-condição `sem_etiqueta`/`pausada`, pois pode ser descalibração, mas exige conferência física de mídia instalada pelo operador. Tampa aberta, falta de ribbon e demais erros impedem iniciar. Envia apenas `~JC` uma vez. Exige trabalho CUPS concluído e duas leituras ociosas em até 60 segundos.

Intenção e resultado são persistidos em `logs/printer-calibration`. Reinício durante a rotina vira conferência pendente; não há repetição automática. Uma calibração incerta bloqueia novas impressões até conferência ou uma nova calibração concluída.

## Legado e diferenças intencionais

Sem feedback e sem token, o agente pode manter `POST /status.php` com `status=concluido` depois de aceite CUPS. **Isso é compatibilidade antiga, não comprovação física.** A configuração de exemplo prioriza o modo moderno e recusa iniciar eventos sem token.

O cache lógico legado conserva o intervalo de 15 minutos para pedidos sem identidade moderna, mas envios incertos ficam bloqueados até conferência. A intenção de envio também é persistida no legado. Não há fallback para TCP ou outra fila após uma resposta de envio perdida: isso poderia imprimir duas vezes ou na impressora errada. Nesta versão todos os envios passam pela fila CUPS configurada; impressora em rede deve ser cadastrada no CUPS.

`/imprimir-etiqueta` só consulta a URL de fila configurada, sem aceitar um host/loja arbitrário via query string. O HTTP local fica em `127.0.0.1:2508` por padrão; exposição à LAN requer configurar `HOST` conscientemente. Respostas com tamanho acima de 4 MB, lotes acima de 1000 itens e quantidades fora de 1–10000 são recusados. Esses limites são explícitos; nenhum conteúdo é truncado para imprimir.

Os cálculos de ocupação/fontes e o ZPL dos renderizadores aprovados foram preservados. Datas formatadas em português permanecem iguais; ISO com fuso usa o fuso local, incluindo o comportamento original de data ISO sem horário representar meia-noite UTC. O serviço de exemplo usa `America/Sao_Paulo`.

Logs rotativos substituem integrações auxiliares de email/PM2 do Node. Não existe atualização automática ou publicação de release nesta etapa. O transporte Windows e renderização de PNG/PDF não fazem parte desta versão local.
