# Relatório de desempenho do upload

## Escopo e segurança

O benchmark usa o agendador simulado em `tools/benchmark_upload_scheduler.py`. Ele não abre sessão no Telegram, não acessa MongoDB e não lê, envia ou exclui arquivos. Os números abaixo medem somente a capacidade de agendamento local; não são promessa de velocidade para uma conexão de 300 Mbps.

## Versão anterior

- Duas filas, por tamanho, e workers pequenos dedicados.
- Pool de upload excluía o bot principal quando havia mais de um bot.
- Por padrão, `MAX_WORKERS=10`, `UPLOAD_CONCURRENCY=8`, quatro partes por arquivo; portanto, trabalhadores e requisições podiam ser confundidos.
- Não havia taxa global periódica nem ETA agregado.

## Versão otimizada

- Fila FIFO única e reivindicação atômica no MongoDB mantida.
- Até 28 arquivos ativos e até 28 requisições/partes em voo são controles distintos.
- 28 bots, inclusive o principal após validar canal, participam da rotação. O padrão inicia com 20 requisições simultâneas (ajustável), entre mínimo 4 e máximo 28. FloodWait e 429 aplicam cooldown ao bot e recuo global; falha isolada de conexão/transporte reduz uma vaga, e falhas de transporte agrupadas reduzem moderadamente. Sucessos elevam a concorrência gradualmente.
- Partes padrão preservadas em 16 MB.
- Logs periódicos incluem MB/s, Mbps, GB/h, uploads ativos, concluídos, pendentes, erros e ETA.

## Observação operacional recebida (09/10/2026)

O log recebido entre aproximadamente 06:07 e 06:15 é o baseline anterior às mudanças de amostragem. O limitador iniciou em 12, subiu até 16 e houve um `ClientOSError` no trecho anterior, que reduziu 14 para 7. As partes de 16 MB registraram em geral 0,13–0,31 MB/s, enquanto as taxas agregadas reportadas oscilaram de zero a 11,72 MB/s. Isso não demonstra ganho de vazão: o teste não foi controlado e a métrica baseada apenas em partes concluídas é naturalmente irregular com blocos grandes; além disso, antes da correção, eventos da fila também reiniciavam a janela. A amostragem foi corrigida para atualizar apenas nas mensagens periódicas. Trate os números agregados deste baseline como indicativos, não como benchmark comparável.

O log atual (06:17–06:19) iniciou novamente em 12, subiu a 14, não mostrou novos erros de transporte e concluiu um arquivo retomado. Entretanto, o cronômetro por parte ainda começava antes de adquirir uma vaga no limitador, incluindo espera na fila; portanto, seus tempos e MB/s misturavam espera e envio. A instrumentação foi alterada para registrar separadamente `espera_slot` e o tempo efetivo de envio. Como esse log não contém essa separação, ele não permite concluir se a vazão atual melhorou em relação ao baseline. É necessário coletar nova amostra com a instrumentação atualizada.

O log atual seguinte (06:21:48–06:22:58) confirma a instrumentação: concorrência efetiva 12, sem novos erros visíveis, com esperas por vaga de até aproximadamente 67 s. Envios de partes de 16 MB que iniciaram após obter vaga levaram em geral 52–55 s (cerca de 0,29–0,31 MB/s); duas retomadas com três partes restantes concluíram em cerca de 54 s. No baseline anterior, uma retomada semelhante de três partes levou aproximadamente 107 s. É um indício favorável para a conclusão de arquivos retomados, mas a amostra é curta, os arquivos e condições não são idênticos, e não constitui comparação controlada. As taxas globais em janelas de 15 s continuam sujeitas a picos/zeros por contagem na conclusão de partes; use tempos por envio e uma janela mais longa para comparar.

No log atual seguinte (06:25–06:26), a concorrência começou em 12 e chegou a 13; um único `ClientConnectorError` reduziu o limite de 13 para 6. Como se trata de falha de estabelecimento de conexão, o controlador foi ajustado para reduzir apenas uma vaga por ocorrência desse tipo; FloodWait, 429, timeouts e erros de transporte durante o envio continuam provocando recuo multiplicativo. As janelas observadas variaram muito e não comprovam a referência de 6 MB/s sustentados.

No log seguinte (06:29:55–06:32:10), a concorrência subiu de 12 para 15 sem novos erros de rede visíveis. A média aritmética das dez janelas consecutivas de 15 s foi aproximadamente 5,21 MB/s (cerca de 41,7 Mbps), próxima da referência histórica informada pelo usuário de 6 MB/s. As janelas individuais oscilaram de 0 a 10,66 MB/s por causa da contabilização em blocos concluídos, mas sua média sobre os 150 s é mais representativa. Uma transferência foi concluída durante a amostra. É um sinal favorável, não um benchmark controlado nem uma garantia de desempenho futuro.

No log de 06:58:30–07:07:45, o limite chegou a 28 e as 40 amostras periódicas de 15 s tiveram média aritmética de aproximadamente 6,60 MB/s (52,8 Mbps), com pico de janela de 16,20 MB/s e grande oscilação. Foram concluídos nove arquivos e o contador de erros permaneceu em um. Às 07:04:19, um `ClientOSError` no bot #9 reduziu o limite global de 28 para 14; a concorrência voltou a subir e terminou em 18. Isso identifica a reação a uma falha isolada como agressiva demais. Foi ajustada para redução de uma vaga por falha isolada de transporte e recuo de um quarto do limite após três falhas de transporte em 15 s. O Bot API também agora preserva e respeita `retry_after` em respostas 429. A medição continua abaixo da meta de 30 MB/s e não separa a rota do Telegram de variação de rede/servidor.

No log das 07:31–09:10, foram identificadas 397 amostras periódicas: média de 6,93 MB/s, pico de janela de 19,18 MB/s e 15 janelas zeradas. Com concorrência limitada a 28, 269 amostras tiveram média de 7,74 MB/s. Houve 24 erros de transporte/timeouts; uma rajada às 07:54:21 fez o controlador anterior colapsar de 28 para 4 vagas. Os registros não permitem atribuir a baixa velocidade apenas ao número de bots, pois não são um teste controlado e não isolam gargalo do Telegram, sistema ou rota.

Como medida operacional conservadora, o padrão foi reduzido para máximo de 24 requisições simultâneas, sem retirar nenhum dos 28 bots da rotação. Falhas isoladas continuam retirando uma vaga; em rajadas, as duas primeiras retiram uma vaga cada e no máximo um recuo adicional moderado é aplicado por janela de 15 s. FloodWait/429 e timeouts continuam com recuo multiplicativo e tratamento próprios. Esse ajuste prioriza evitar colapso de concorrência; o log histórico não é uma medição da versão ajustada e não demonstra aumento de velocidade.

## Meta de 30 MB/s

30 MB/s equivalem a aproximadamente 240 Mbps, ou 80% de um link nominal de 300 Mbps. A amostra operacional das 07:31–09:10 teve média de 6,93 MB/s, ainda distante dessa meta. O padrão após o ajuste inicia em 20 requisições e limita a 24, com adaptação a erros/limites; os 28 bots continuam disponíveis para rotação. Isso não é uma previsão linear: se a taxa por requisição cair com a concorrência, a meta não será atingida apenas usando mais bots. A validação deve observar média móvel por alguns minutos, erros e estabilidade, sem uploads sintéticos em produção.

## Responsividade de navegação em pastas

As listagens do FTP usam MongoDB para montar a árvore virtual. A visibilidade de subpastas fazia uma consulta de descendentes por pasta, sequencialmente. Agora essas verificações são executadas em lotes limitados a oito consultas simultâneas, sem alterar o esquema. Além disso, a recuperação de arquivos do stage percorria o disco sincronamente no event loop a cada cinco segundos. A travessia e coleta de metadados passou para uma thread, com uma chamada de `stat` por arquivo, e o intervalo padrão foi aumentado para 30 segundos (`STAGE_SCAN_INTERVAL`, mínimo de 5 s). Isso deve reduzir bloqueios de FTP e pressão de I/O; a descoberta de arquivos copiados manualmente para o stage pode demorar até um ciclo.


## Como reproduzir com segurança

```powershell
.\.venv\Scripts\python.exe .\tools\benchmark_upload_scheduler.py --levels 1,4,8,16,28
```

## Resultado observado (simulação local, 56 partes de 16 MB, 5 ms por parte)

| Concorrência | Pico observado | Tempo | MB/s | Mbps | GB/h |
|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 0,877 s | 1.022,1 | 8.176,8 | 3.593,3 |
| 4 | 4 | 0,218 s | 4.117,3 | 32.938,1 | 14.474,8 |
| 8 | 8 | 0,108 s | 8.277,0 | 66.215,8 | 29.098,7 |
| 16 | 16 | 0,062 s | 14.386,0 | 115.088,3 | 50.575,9 |
| 28 | 28 | 0,031 s | 28.915,1 | 231.320,6 | 101.654,5 |

Esses valores são taxa de agendamento em memória, logo deliberadamente não representam rede, disco ou limites do Telegram. A comparação válida é estrutural: a versão anterior iniciava com oito requisições e dez arquivos; a nova alcançou os cinco níveis, incluindo pico de 28, sem efeito externo.

Para validar operação real, execute primeiro em canal de teste e com arquivos descartáveis. Ajuste `UPLOAD_CONCURRENCY_START` (padrão 20), observe logs de `FloodWait`, 429 e timeout, e reduza/incremente controladamente. A amostra operacional acima veio de uploads reais já em andamento, não de um benchmark controlado. O benchmark local do escalonador não mede rede ou Telegram e não deve ser comparado como throughput de upload.

## Ajuste operacional de concorrência — 2026-10-16

O log operacional recebido entre 12:55 e 12:59 confirmou 28 bots conectados, mas apenas 10 arquivos em envio e até 20 requisições ativas. A configuração local carregada tinha `MAX_WORKERS=10` e `PART_WORKERS_PER_FILE=2`: cada arquivo ocupava duas requisições em paralelo, limitando o conjunto normal a 20 partes simultâneas e deixando parte da pool de bots sem uso. `UPLOAD_CONCURRENCY=24` também impedia usar 28 requisições, apesar dos 28 bots.

As janelas de taxa oscilaram de 0 a 13,77 MB/s; a última amostra mostrou 1,06 MB/s com 19 requisições ativas e limite adaptativo 22. Envios individuais de 16 MB levaram aproximadamente 53–69 s (0,23–0,30 MB/s em várias partes). Não houve `FloodWait`, 429 ou falha de transporte explícita nesta amostra. Os zeros são compatíveis com a métrica baseada em partes concluídas e não demonstram pausa total da rede.

Configuração operacional alterada para `MAX_WORKERS=28`, `PART_WORKERS_PER_FILE=1`, `UPLOAD_CONCURRENCY=28`, `UPLOAD_CONCURRENCY_START=24` e máximo 28. O mínimo adaptativo permanece 4. Isso permite até 28 arquivos e 28 requisições simultâneas, distribuindo uma parte por arquivo. `CHUNK_SIZE_MB=16` permanece inalterado. A mudança aguarda reinício operacional para entrar em vigor; nenhum upload real foi iniciado nesta sessão.

Isto remove o limite local identificado e amplia concorrência em até 40% sobre as 20 requisições observadas. Não comprova aumento de vazão nem aproxima, por si só, a meta de 30 MB/s. Após reinício autorizado, compare médias de pelo menos cinco minutos, taxa por parte, erros, FloodWait/429 e memória; reverta para 20–24 se a taxa agregada cair ou os erros aumentarem. O host tinha cerca de 5,7 GiB de RAM livre na inspeção. A leitura antecipada pode manter até aproximadamente 896 MiB de payload em 28 workers; monitore a memória durante a validação. Suíte local em Python 3.14.7: `204 passed, 1 skipped`; valida somente código isolado, sem acesso Telegram/Mongo e sem benchmark de rede.

## Log operacional 13:11–13:29 — 2026-10-16

O log anexado mostra a configuração anterior já aplicada e reiniciada: 28 bots ativos, 28 arquivos enviando, limitador adaptativo partindo em 24 e alcançando 28 por volta de 13:16. Foram 69 amostras de 15 s, média aritmética de 8,19 MB/s (65,5 Mbps; aproximadamente 28,8 GB/h), com janelas de 0 a 14,89 MB/s. O arquivo registrou 535 partes concluídas durante o trecho; partes de 16 MiB com frequência levaram cerca de 50–60 s por envio (aproximadamente 0,27–0,32 MB/s). Os estados periódicos reportaram zero erros.

Há também 100 avisos `Fila restaurada preservou registro sem arquivo local` durante a recuperação. O código preservou esses registros, o que evita perda de estado, mas eles não são fontes enviáveis enquanto o arquivo não existir no disco/stage. Podem inflar a quantidade de pendências/ETA; não explicam a taxa por requisição dos 28 uploads ativos. Não foi feita limpeza no Mongo nem exclusão local.

Não foi encontrado `FloodWait`, 429, timeout ou erro de transporte no trecho. As 28 transferências simultâneas e o limite 28 foram atingidos; portanto, a amostra não aponta falta de bots ou teto local de concorrência como causa imediata. As janelas de 15 s são irregulares porque os bytes só são contabilizados quando uma parte inteira termina; zero numa janela não significa ausência de tráfego.

O envio desse caminho usa `send_document_bot_api` (HTTP multipart), não `sequential_save_file` do Pyrogram; assim, o `UPLOAD_PART_WORKERS=1` para as partes MTProto de 512 KiB não explica estes tempos. Não foi encontrada evidência suficiente de um gargalo corrigível no código local. Elevar acima de 28 requisições ou abrir múltiplas requisições por bot seria experimento novo, sem suporte nessa amostra, e aumentaria pressão/risco de 429. Nenhum ajuste de concorrência ou tamanho foi feito a partir deste log.

A velocidade de Speedtest é para outro destino e não prova a vazão da rota até a Bot API do Telegram. Próximo diagnóstico seguro: comparar, numa janela estável, taxa por bot e latência/resolução/conexão do endpoint de upload, uso de CPU/disco e throughput sustentado; qualquer experimento além de 28 deve ocorrer em janela autorizada, com arquivos de teste não destrutivos e monitoramento de 429/FloodWait/memória. Estes números são observação de produção, não benchmark controlado nem garantia de melhoria.

## Segundo log operacional 16:21–17:37 — 2026-10-16

A nova amostra contém 301 janelas periódicas de 15 s (cerca de 75 min), média de 8,64 MB/s (69,1 Mbps; 30,4 GB/h), máximo de janela 17,02 MB/s e mínimo 0 por quantização da métrica. Com 28 uploads ativos, o limitador chegou a 28 em aproximadamente cinco minutos e permaneceu próximo do teto. Foram 2.463 eventos de conclusão de parte logados, com mediana de 54,4 s por parte; o log também mostra 23 conclusões de arquivos. Comparada à amostra anterior (8,19 MB/s), há diferença de cerca de +5,5%, mas horários/arquivos diferentes impedem atribuir ganho à configuração.

Ocorreram 10 erros transitórios de transporte (`ConnectionTimeoutError`, `ClientOSError` e `ClientConnectorError`) e reduções breves do limitador; a concorrência recuperou até 28. Não há ocorrência real de `FloodWait` ou 429: as buscas textuais por `429` confundem esse número com timestamps/IDs. O contador periódico de erros ficou em zero, compatível com falhas recuperadas por retry, e não significa ausência de falhas transitórias.

A amostra reforça que aumentar a concorrência não está evidenciado como correção: o teto já foi atingido. As falhas de transporte são uma linha de investigação, mas são esparsas (10 em aproximadamente 75 min) e não bastam para explicar a taxa persistentemente baixa. Não alterei código/configuração e não iniciei teste real. Próximo passo técnico é instrumentar/observar DNS, conexão, TLS, envio e resposta HTTP por bot e correlacionar com uso de CPU/disco/placa de rede; separar a taxa por estágio antes de decidir alteração. O teste deve respeitar janela operacional e limites do Telegram.
