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
