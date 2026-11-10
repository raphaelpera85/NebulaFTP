# Biblioteca STRM

O botão **Gerar Biblioteca STRM** consulta os arquivos concluídos no MongoDB e
monta a biblioteca na pasta configurada em `STRM_OUTPUT_DIR` (por padrão,
`strm_library` ao lado de `generate_strm.py`).

Quando um upload de mídia executável termina, o worker cria também o STRM
individual dessa mídia depois de confirmar no MongoDB a atualização do estado
`completed` (um registro correspondente) e suas partes. O caminho usa o `parent` resolvido do registro e segue a mesma
estrutura relativa ao usuário da biblioteca. O link usa `/stream?id=<id>` e
`STREAM_HOST`/`STREAM_PORT`, como o gerador incremental. Esse caminho não varre
a biblioteca inteira. Arquivos não executáveis (incluindo NFO e legendas) não
recebem STRM automaticamente.

O STRM é complementar ao upload: uma falha de gravação ou conflito é registrada
sem rebaixar o upload concluído. O arquivo original não é excluído por causa de
uma falha STRM; a política normal de staging só pode removê-lo após a conclusão
confirmada do upload. A geração completa pelo botão pode reconciliar links
ausentes depois.

Cada arquivo `.strm` contém um link `/stream?id=<id-do-mongodb>`. Ao gerar a
biblioteca, o Nebula percorre os `.strm` já existentes no destino e valida o
endpoint e o ID contra os arquivos concluídos encontrados no MongoDB.

- Links válidos são preservados sem regravação.
- Um link válido para a mesma mídia, mesmo em outro caminho da biblioteca,
  impede a criação de uma segunda cópia.
- Se ainda não houver link válido para a mídia, um link inválido no caminho
  esperado é atualizado. Na execução padrão, links inválidos fora do caminho
  esperado são mantidos e contabilizados no resumo.
- Arquivos ausentes são criados; a geração padrão não remove arquivos antigos.
- Referências a sidecars NFO (nomes contendo `.nfo` sem extensão final de mídia
  executável) não geram `.strm`. Nomes que contenham `.nfo` mas terminem em uma
  extensão de mídia reconhecida continuam elegíveis.
- Para filmes cujo nome no MongoDB corresponda exatamente a `movie.info.<hash>`
  (8–64 caracteres hexadecimais), o arquivo `.strm` usa o título da pasta do
  filme. O documento do MongoDB não é alterado. Um único link `.strm` existente
  com esse nome genérico só é renomeado após validar seu ID; links ambíguos,
  inválidos ou em outro diretório permanecem intactos.
- Conflitos com links válidos de outra mídia ou caminhos que não sejam arquivos
  regulares são preservados e informados, sem sobrescrita.
- Se documentos concluídos do MongoDB resultarem no mesmo destino STRM, o
  gerador preserva o destino existente e contabiliza conflito; não escolhe um
  registro arbitrariamente nem altera os documentos MongoDB.

O resumo informa arquivos gerados, links atualizados, mídias preservadas ou
renomeadas, links inválidos examinados e conflitos. A função opcional de limpeza
de STRMs obsoletos continua separada da execução padrão do botão e pode remover
arquivos inválidos ou obsoletos que não estejam associados a mídias concluídas.

## Reclassificações confirmadas — 2026-10-21

O campo `parent` do documento de mídia no MongoDB é a fonte da categoria usada
na próxima geração da biblioteca. Quando o operador confirma uma categoria
incorreta, corrija o `parent` antes de regenerar os STRMs; mover apenas o arquivo
local não é persistente.

- **Prisma**: dois documentos concluídos foram movidos de
  `/raphael/Animações/Prisma/Season 01` para
  `/raphael/Series/Prisma/Season 01`. Estados, tamanhos e partes Telegram foram
  preservados. O STRM existente foi movido para `Series` sem alteração do
  conteúdo; os dez arquivos da pasta mantiveram os hashes.
- **Y - O Último Homem**: dez documentos concluídos tiveram somente o `parent`
  alterado de `Animações` para `Series`, conforme confirmação do usuário e NFO
  local (`Drama`, `Sci-Fi & Fantasy`). Os STRMs já estavam em `Series`.
- **Hell's Paradise**: os 25 documentos concluídos já apontavam para
  `Animações`; nenhum documento ou STRM foi alterado. A cópia sob `Series` não
  contém STRM e tem dois NFOs divergentes, portanto seus sidecars foram mantidos
  sem sobrescrita ou exclusão.
- Auditoria pós-correção de `strm_library`: 26.948 STRMs; todos resolveram para
  documentos Mongo `completed`, sem IDs repetidos, links inválidos ou diferença
  entre categoria física e `parent`.
- Backups mínimos dos `parent` anteriores estão em
  `backups/mongo_prisma_parent_20261021.jsonl.gz` e
  `backups/mongo_y_ultimo_homem_parent_20261021.jsonl.gz`. Nenhum documento ou
  arquivo duplicado foi excluído na correção de Prisma, Y - O Último Homem e
  Hell's Paradise. `D:\midias` não foi alterada naquela operação; a alteração
  posterior está descrita abaixo.

### All of Us Are Dead e Solteiros, Ilhados e Desesperados — 2026-10-21

- O usuário confirmou os dois títulos como séries. Um documento Mongo
  `completed` de cada título teve somente a categoria de `parent` alterada de
  `Doramas` para `Series`; IDs, `status`, `parts` e `size` foram preservados.
- As árvores completas de `strm_library/Doramas/<título>` foram movidas para
  `strm_library/Series/<título>`, mantendo STRM e sidecars. O ID de cada STRM
  foi conferido contra o `_id` Mongo correspondente.
- Em `D:\midias`, havia 11 links de All of Us Are Dead e 52 de Solteiros,
  Ilhados e Desesperados duplicados entre as categorias. Todos tinham mesmos
  caminhos relativos e bytes; os duplicados em `Doramas` foram removidos e as
  cópias idênticas de `Series` foram preservadas. Nenhum arquivo não-STRM
  existia nessas árvores de origem.
- Pós-validação: os dois documentos continuam `completed` em `Series`, os dois
  STRMs da biblioteca gerada resolvem para seus IDs Mongo, há 11 e 52 links em
  `D:\midias/Series`, e não restam árvores desses títulos em `Doramas` nos dois
  locais.
- Os links de `D:\midias` são entradas distintas de provedor. A busca local não
  encontrou ali os episódios correspondentes aos documentos Mongo
  (`All of Us Are Dead - S01E03` e `Solteiros, Ilhados e Desesperados - S01E01`);
  eles existem em `strm_library/Series`. Nenhum link novo foi criado em
  `D:\midias`, para não substituir sua origem de reprodução sem instrução.
- Backup mínimo dos dois `parent` anteriores:
  `../backups/mongo_series_category_parent_20261021.jsonl.gz`. O esquema do
  MongoDB permaneceu igual.
