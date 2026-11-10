# Guia M3U → STRM

A guia **M3U / IPTV** da GUI mantém uma playlist M3U sincronizada com a raiz
monitorada. O destino padrão é `D:\midias`, cuja estrutura observada em
2026-11-10 é:

- `Filmes/`
- `Series/`
- `Novelas/`
- `Animações/`
- `Doramas/`

A sincronização cria somente links `.strm` e não baixa a playlist inteira para
o disco. Cada link contém a URL de um filme ou episódio.

## Configuração pela GUI

1. Abra a guia **M3U / IPTV**.
2. Cole a URL no campo de playlist. O formato copiado com `@url:` e crases é
   aceito; o prefixo e os delimitadores são removidos antes da requisição.
3. Confirme `D:\midias` como destino.
4. Escolha o intervalo em minutos e mantenha **corrigir recuperação** ativo.
5. Use **Testar URL** ou **Executar agora**; depois use **Iniciar automático**.
6. Na busca, digite parte do título, episódio ou categoria, selecione uma ou
   mais linhas e use **Priorizar selecionada**.

A URL fica em `m3u_strm_config.json`, o catálogo pesquisável em
`m3u_strm_catalog.json` e as prioridades em `m3u_priority.json`. Há um modelo
sem credenciais em `m3u_strm_config.example.json`; copie-o para
`m3u_strm_config.json` e preencha a URL somente na máquina de destino. Esses
arquivos são locais e não devem ser versionados quando contiverem credenciais.

## Categorização

O parser usa `group-title`, nome exibido e caminho da URL para classificar
entradas de filme/série. As categorias são mapeadas para as pastas existentes:

| Categoria | Exemplos de grupos reconhecidos |
| --- | --- |
| `Filmes` | filme, filmes, movie, movies, cinema |
| `Series` | série, series, seriado, TV show, caminhos `/series/` |
| `Novelas` | novela, novelas, telenovela, soap |
| `Animações` | anime, animes, animação, desenho, cartoon, infantil |
| `Doramas` | dorama, doramas, K-drama, drama coreano |

Entradas de TV ao vivo sem caminho `/movie/` ou `/series/` são ignoradas pelo
gerador. Um item de série é criado em `Categoria/Título/Season NN/`; um filme
é criado em `Filmes/Título (AAAA)/` quando o ano estiver disponível.

## Recuperação e retomada

A recuperação consulta registros Mongo marcados como `recovery_required` e
`failed_reason=local_path_missing`. Quando encontra a entrada correspondente
na playlist, recria o `.strm` no `parent` persistido, inclusive quando esse
`parent` é um caminho virtual como `/raphael/Series/...`.

A recuperação não remove os arquivos parciais `.download*` nem altera os
metadados de partes do download. O Downloader usa esses parciais e seus offsets
para continuar do ponto salvo. O registro de recuperação só é limpo depois que
o `.strm` é recriado com sucesso.

## Prioridade

A busca da guia combina o catálogo M3U com `.strm` já existentes na raiz. Ao
priorizar uma mídia, o caminho relativo e o título são persistidos. O Downloader
aplica a prioridade na ordenação dos lotes e grava `priority=1` no documento de
fila do Nebula; a fila de upload também ordena documentos prioritários antes dos
itens comuns.

Prioridade não interrompe uma transferência em andamento. Ela vale para a
próxima mídia elegível e mantém a retomada existente quando houver um parcial.

## Validação executada

- Estrutura local somente leitura de `D:\midias` conferida em 2026-11-10:
  `Filmes` 4.100 STRMs, `Series` 155.308, `Novelas` 33.429,
  `Doramas` 1.359 e `Animações` 24.
- A playlist configurada respondeu `403` para User-Agent genérico e `200` com
  `VLC/3.0.20 LibVLC/3.0.20`. O parser leu 204.189 entradas VOD sem grupos
  desconhecidos: Filmes 14.219, Series 117.864, Animações 14.309, Novelas
  52.662 e Doramas 5.135.
- O parser também passou a tratar atributos M3U vazios como string vazia.
- `tests/test_m3u_strm_generator.py` e `tests/test_strm_downloader.py`:
  79 testes focados aprovados.
- Suíte completa `../.venv/Scripts/python.exe -m pytest -q --disable-warnings`:
  273 aprovados, 1 ignorado e 1 aviso de cache do pytest no Windows.
- `py_compile` passou para `tools/m3u_strm_generator.py`,
  `tools/strm_downloader.py`, `main.py` e `gui.pyw`.
- Não foi executada uma sincronização que escrevesse STRMs em `D:\midias`, nem
  download, upload ou alteração operacional no Mongo durante esta implementação.
