# Validação da portabilidade Python

## Base

- Origem: cópia local do agente Node `stockflow`, versão 2.1.0, commit `e61f2799cd9f89df82c5dd0f989fa99e4898a816`.
- Destino: pasta nova `raspeberry-stockflow`, implementação de desenvolvimento `2.1.0.dev1`.
- Hardware alvo: Raspberry Pi 1 Model B+ V1.2, ARMv6 de 32 bits, Raspbian 13, Python 3.13.5.
- Execução automatizada local: Windows, Python 3.15.2. Sintaxe também verificada com parser Python 3.11. Não equivale a executar a suíte no Python 3.13/ARMv6.
- Nenhuma conexão SSH, chamada ao servidor de produção, impressão física ou alteração no original durante esta implementação.

## Layout

`tests/fixtures/golden.json` contém oito exemplos sintéticos gerados diretamente pelos renderizadores Node, sem importar o serviço de impressão. A comparação exige igualdade exata do ZPL e da geometria do QR:

- Produto curto, médio e longo.
- Campos extensos.
- Código `0`, payload longo e caracteres acentuados.
- Layout Elgin existente.

O teste longo preserva os caracteres e aumenta a altura do nome. Os testes rejeitam campos impossíveis de acomodar e QR vazio. Cobrem escape de comandos ZPL, UTF-8, escuridão e geometria. A prévia local usa o mesmo renderer do envio.

O símbolo QR habitual mantém a ampliação aprovada: versão 1, módulo de 6 dots, 126 × 126 dots, origem 323,316. A reserva ideal de 4 módulos de silêncio entra na área de divisórias do rodapé; essa limitação já existia no layout aprovado fisicamente do Node e não foi redesenhada nesta migração. Igualdade de ZPL não demonstra legibilidade em um novo equipamento.

## Simulações

Execute `python -m unittest discover -s tests -v` na raiz. A suíte usa diretórios temporários, transporte falso e HTTP apenas em loopback; não usa banco, USB, CUPS ou servidor externo.

Resultado local em 09/11/2026: **31 testes aprovados**. Compilação dos módulos e geração das duas prévias ZPL também concluídas. O ciclo principal do daemon foi exercitado com transporte/API simulados e encerramento sem repetir o envio.

Cobertura: parsing estrito de `~HS`/contador, quantidade exata, duas leituras ociosas, contador divergente, outro trabalho CUPS, perda da resposta após intenção persistida, recuperação de reinício, mídia ausente por espera longa, suspensão para calibração, deduplicação, expiração somente do legado concluído, ACK de evento divergente, repetição de eventos sem repetir impressão, calibração executada uma vez e recuperação incerta, filtro de loja, bloqueio de URLs/redirects, heartbeat com USB ocupado, trava de instância, HTTP local, jitter de 1 segundo, quantidades inválidas e caminhos do leitor QR.

## Ainda precisa de validação no Raspberry

- Instalação dos pacotes, fila RAW, permissões udev/grupos e interação CUPS/libusb no Raspbian instalado.
- Uma etiqueta real com exatamente +1 no contador e evento final recebido pelo servidor.
- Tampa, pausa, falta de etiqueta, ribbon realmente no fim, desconexão USB e reinício durante envio.
- Calibração, retomada do mesmo pedido e ausência de etiquetas duplicadas.
- Desempenho/uso de memória do daemon e do leitor no ARMv6 com operação prolongada; dependências opcionais do leitor.
- Funcionamento físico GC420t e Elgin; confirmação USB nesta implementação continua específica à Zebra ZD220 identificada por VID/PID e serial.
- Transporte de impressora e captura do leitor no Windows, em etapa futura.

Os erros USB e o registro de subtensão observados no Raspberry antes desta portabilidade precisam ser acompanhados nos testes físicos. Não foram alteradas configurações de energia/USB para mascarar esses erros.

## Instalação e teste físico posterior

Em 09/11/2026, a instalação foi realizada no Raspberry. Os 31 testes passaram também no Python 3.13.5/ARMv6 e uma etiqueta física foi confirmada (contador 12155 → 12156, CUPS concluído e usuário confirmou a saída). Consulte [registro da instalação](instalacao-raspberry.md) para o estado atual e os testes ainda pendentes. As restrições anteriores descrevem a etapa de implementação local.
