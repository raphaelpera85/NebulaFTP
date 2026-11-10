# Fila de upload e painel da GUI

O Nebula mantém no MongoDB o estado persistente de cada arquivo. `queued` é
pendente de envio, `staging` está preparado/aguardando worker e `uploading` é
transferência ativa. O scanner só encaminha documentos `queued` com caminho
local válido e sem `recovery_required`; documentos sem fonte local ou com
destino duplicado ficam preservados para recuperação/revisão e não são enviados
automaticamente.

Quando a origem local de um item ativo está ausente (`recovery_required` com
`failed_reason=local_path_missing`), o STRM correspondente é a fonte para
recuperação. O Downloader tenta esses links antes da fila normal e os mantém
na biblioteca até o reupload ao Telegram chegar a `completed`. Ao registrar o
arquivo recuperado, atualiza o mesmo documento Mongo e preserva `parts` e o
progresso anterior; não cria uma identidade paralela nem reinicia partes.
Falhas de consulta/download ou conflito de registro mantêm o STRM disponível
para nova tentativa.

A seção **Uploads Ativos / Fila de Envio** da GUI consulta esses três estados.
O cabeçalho totaliza `Ativos`, `Fila` e `Recuperação`. Para manter o painel limpo
e evitar sobrecarga com filas extensas, a lista exibe cards detalhados apenas
para os uploads atualmente em transmissão (`uploading`) e para mídias que
exigem intervenção (`recovery_required`). Os itens pendentes normais da fila
são apresentados exclusivamente no contador numérico do cabeçalho.

## Diagnóstico seguro

1. Se `Ativos: 0` e `Fila: N`, não há transferência em andamento. O total de
   mídias na fila é indicado no contador numérico do cabeçalho.
2. Se a linha disser `Aguardando arquivo local`, confira se o arquivo original
   voltou ao caminho esperado. Não limpe o documento Mongo nem reinicie o
   progresso para fazê-lo desaparecer.
3. Se disser `Duplicado preservado`, revise o conflito de destino antes de
   qualquer ação.
4. Só marque um arquivo como recuperável quando a origem local e a identidade
   do documento estiverem confirmadas. A UI é somente leitura para esses dados.

## Fontes

- Estado persistente, restauração e scanner: `main.py`.
- Consulta e renderização do painel: `gui.pyw`.
- Regressão da descrição de estados: `tests/test_gui_upload_queue.py`.
