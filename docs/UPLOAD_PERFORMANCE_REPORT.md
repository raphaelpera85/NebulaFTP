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
