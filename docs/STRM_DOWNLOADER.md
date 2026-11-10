# Inicialização do STRM Downloader em bibliotecas grandes

O STRM Downloader percorre as fontes configuradas em `monitor_paths.json` e
classifica arquivos `.strm` e mídias locais antes de baixá-los para uma pasta de
stage. Bibliotecas muito grandes podem conter centenas de milhares de itens.

## Varredura progressiva

O processamento usa lotes de até 250 candidatos. Assim que o primeiro lote é
encontrado, o downloader começa a validar e processar esses itens enquanto
continua a varredura. A varredura respeita a prioridade de categorias (`Animações` > `Filmes` > `Séries` >
`Doramas` > `Novelas` > `Porno`) e a ordem estritamente alfabética (A–Z) dentro
de cada categoria. O lote é ordenado mantendo primeiro os itens de recuperação
(`recovery_required`), seguidos pela ordem alfabética da categoria. Arquivos parciais presentes no stage continuam sendo priorizados
dentro do lote.

Durante varreduras longas, os logs informam periodicamente quantas pastas e
arquivos já foram verificados, quantos candidatos foram encontrados e quantos
lotes foram processados. A GUI mostra esses contadores no painel de mídia e
troca para “Validando primeiro lote” quando os primeiros candidatos ficam
disponíveis.

## Recuperação de upload sem fonte local

Registros ativos que indicam `recovery_required=true` e
`failed_reason=local_path_missing` recebem prioridade antes da varredura normal.
O Downloader localiza o STRM pelo ID do stream ou pelo destino Mongo, baixa
novamente para o stage e reativa o mesmo documento da fila sem substituir
`parts`. O STRM original é preservado enquanto o upload recuperado está pendente
ou ativo e só pode ser removido quando a mídia já aparece como concluída.
Falhas de consulta/download ou conflito de registro mantêm o STRM disponível
para nova tentativa.

## Correção automática de categoria

Antes de iniciar o download, o Downloader extrai o ID da URL do `.strm` e
consulta diretamente o registro `completed` (ou recuperação prioritária) no
MongoDB. Se o `parent`
persistido indicar outra categoria conhecida (`Animações`, `Filmes`, `Series`,
`Doramas`, `Novelas` ou `Porno`), o link é movido para o caminho correspondente
sob a raiz monitorada; o conteúdo e o nome do `.strm` são preservados. O caminho
corrigido também determina o destino do download, evitando reenfileirar a mídia
na categoria antiga.

A movimentação só ocorre dentro da raiz configurada e usa criação de hard link
sem sobrescrita do destino, seguida da remoção da origem. Em caso de colisão,
link simbólico, categoria não reconhecida, falha na consulta ao Mongo ou erro de
disco, o original permanece e o item não é baixado para uma categoria incerta.
O modo `--dry-run` apenas registra o caminho que seria usado e não cria destino.
Recuperações prioritárias também procuram links pelo ID quando o caminho
canônico não existe. Se a URL já contiver ID, ele precisa corresponder ao
documento de recuperação para o link ser aceito.

Esta reconciliação não altera o MongoDB nem decide a categoria pelo título. O
`parent` atual de cada mídia concluída (ou em recuperação prioritária) é a fonte
da categoria; outros registros pendentes, links sem ID resolvível e documentos
com categoria fora do conjunto conhecido não são reclassificados
automaticamente. Se a consulta de um link com ID falhar, o download é adiado
para evitar gravar a mídia em categoria potencialmente errada.

## Capacidade e retomada entre Stages

Antes de baixar ou mover uma mídia, o Downloader verifica todos os Stages
configurados. Mantém o Stage atual quando ele ainda satisfaz a reserva de espaço;
caso contrário, seleciona outro Stage que comporte o arquivo. Se houver partes
de download no Stage anterior, copia os arquivos `.download*` para o destino
alternativo e confere a cópia, mantendo os originais durante a transferência.
Uma colisão com conteúdo diferente ou uma falha de cópia mantém os originais e
não inicia a transferência no destino alternativo. As cópias parciais são
removidas somente depois que o arquivo final foi montado e validado. Se nenhum
Stage tiver capacidade suficiente, a espera reavalia todos periodicamente.

## Quando não começa uma transferência

- Confirme se o processo STRM Downloader está ativo e se a fonte listada no log
  corresponde à biblioteca esperada.
- Se a varredura termina com zero candidatos, confira a fonte e os filtros.
- Se aparecem candidatos, mas não há transferência, procure nos logs razões de
  mídia já concluída/ativa, URL inválida, falha ao consultar tamanho ou falta
  de espaço no stage.
- Não inicie um segundo downloader para contornar uma varredura longa; aguarde
  os contadores ou pare o processo existente pela interface antes de reiniciar.

## Nomes acentuados e recusas HTTP

O scanner inclui arquivos `.strm` pelo sufixo e não altera os acentos dos
caminhos. Na ordenação alfabética, remove diacríticos apenas da chave de
ordenação, então `Àrvore` fica junto de `Arvore`. Uma resposta HTTP 403 ou 406
vem do servidor remoto, não da comparação local do nome. O `FailureTracker`
mantém a quarentena individual de 406, mas esse código não pausa os outros
episódios da mesma série; 401, 403, 404 e 410 ainda podem acionar o bloqueio por
série após três falhas consecutivas. Consulte o log e
`~/.nebula_strm_failures.json` para distinguir espera na fila, quarentena por
falha e recusa remota. Uma falha antiga sai da quarentena após o cooldown; ela
não prova o estado de uma tentativa atual.

Uploads Telegram são independentes desta varredura. Os documentos `queued`
marcados `recovery_required` não podem ser enviados até que os arquivos locais
originais estejam novamente acessíveis nos caminhos registrados.
