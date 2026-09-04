# Graph Report - NebulaFTP-master  (2026-08-27)

## Corpus Check
- 147 files · ~417,610 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1475 nodes · 2418 edges · 155 communities (135 shown, 20 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 85 edges (avg confidence: 0.69)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `c91ddbcd`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- feed_ftp.py
- asyncio
- ControlPlane
- accounts_manager.py
- Remover arquivo de instalação
- test_control_plane_p1.py
- 🪟 Windows (Nativo)
- RuntimeError
- test_audit_fixes.py
- Server
- build_content_metadata_fix_plan.py
- ConnectionConditions
- validar_midias_ia.py
- Writer
- server.py
- tg.py
- main.py
- clean_already_sent.py
- pathio.py
- MongoDBPathIO
- upload_worker_parallel
- audit_all_media_ffprobe.py
- strm_downloader.py
- README.md
- test_check_deps.py
- process_strm_item
- StreamIO
- main
- test_strm_downloader.py
- destination_for
- ECOSYSTEM.md
- 📱 Configuração do Telegram
- AvailableConnections
- resolve_media_parent
- iter_strm_files_prioritized
- episode_identity
- <img src="https://github.com/samucamg/NebulaFTP/blob/master/img/logo_nebula_sftp.png" alt="Logo Nebula SFTP" width="250px">
- Permission
- restore_mongo_from_telegram.py
- Path
- final_all_fix.py
- final_careful_fix.py
- fix_cosmetic_issues.py
- handle_http_client
- fix_mismatched_media_to_true_content.py
- multi_frame_visual_ai_probe.py
- test_realign_stream_titles.py
- comprehensive_fix_v2.py
- fix_remaining_garbage.py
- fix_remaining_no_year.py
- get_cache_dir
- audit_and_fix_all_drive_n.py
- direct_probe_porno_and_ai_realign.py
- fast_parallel_restore.py
- fix_titles_clean_rebuild.py
- fix_titles_final.py
- fix_titles_precise.py
- full_manual_stream_repair.py
- identify_anime_scenes.py
- 🤖 Passo 2: Criar Bot(s)
- final_cleanup.py
- final_cosmetic_cleanup.py
- final_garbage_cleanup.py
- final_quarantine.py
- CompactingFileHandler
- extensive_deep_clean_and_ai_realign.py
- fix_all_title_content_mismatches.py
- full_scan_and_restore.py
- realign_all_media_to_true_stream_content.py
- verify_and_fix_final.py
- comprehensive_cleanup.py
- NebulaFTP — Security & Audit Notes
- 📋 Passo 1: Obter API Credentials
- final_filename_cleanup.py
- final_moves.py
- fix_garbage_dirs.py
- fix_kaiji_series.py
- fix_last_dirs.py
- fix_last_two.py
- MongoDBMemoryIO
- audit_embedded_media_titles.py
- fix_database_categories.py
- purge_and_verify_clean_library.py
- realign_library_with_local_ai.py
- realign_porno_to_true_movies.py
- realign_with_lm_studio_ai.py
- restore_fast_single_bot.py
- 🚀 NebulaFTP
- 🗺️ Roadmap
- <img src="https://raw.githubusercontent.com/samucamg/NebulaFTP/refs/heads/master/img/logo_nebula_cloud.png" alt="Logo Nebula FTP" width="300px">
- 🚀 Instalação
- final_fixes.py
- parse_range
- download_strm_multipart
- ai_realign_porno_frames.py
- 🚀 Guia de Instalação: NebulaFTP Community (Docker)
- 🏗️ Arquitetura e Integração
- 🔐 NebulaSFTP
- 💼 Casos de Uso
- ❓ Problemas Comuns
- AbstractAsyncLister
- .retr
- 💬 Suporte
- 🎯 Casos de Uso
- detect_title_content_mismatches.py
- fast_local_ai_realign.py
- normalize_library_mongo.py
- normalize_library_mongo_enhanced.py
- image_to_base64
- 📄 Licença
- 🛠️ Requisitos
- get_channel_id.py
- quarantine_garbage_copies.py
- 🔧 Recursos Técnicos
- 📊 Demonstração
- 📖 Documentação
- 🚀 Início Rápido
- setup_database.py
- apply_visual_recognition_fixes.py
- deep_verify_all_media.py
- diagnose_real_content_mismatches.py
- force_restore_filmes.py
- normalize_file_parts_and_indexes.py
- test_local_ai_smart.py

## God Nodes (most connected - your core abstractions)
1. `Server` - 64 edges
2. `ControlPlane` - 37 edges
3. `MongoDBPathIO` - 27 edges
4. `main()` - 23 edges
5. `strm_worker()` - 22 edges
6. `main()` - 19 edges
7. `ConnectionConditions` - 18 edges
8. `upload_part_with_retries()` - 18 edges
9. `Remover arquivo de instalação` - 17 edges
10. `hash_password()` - 16 edges

## Surprising Connections (you probably didn't know these)
- `test_feeder_supervisor_uses_argv_and_rejects_outside_roots()` --uses--> `FeederSupervisor`  [INFERRED]
  tests/test_control_plane_p1.py → control_plane.py
- `ControlPlane` --uses--> `MongoDBPathIO`  [INFERRED]
  control_plane.py → ftp/pathio.py
- `test_canonical_p1_routes()` --uses--> `ControlPlane`  [INFERRED]
  tests/test_control_plane_p1.py → control_plane.py
- `test_legacy_plaintext_compare_still_works_during_migration()` --calls--> `verify_password()`  [INFERRED]
  tests/test_audit_fixes.py → ftp/auth.py
- `test_pathio_lru_caps_size()` --calls--> `BoundedLRUCache`  [INFERRED]
  tests/test_audit_fixes.py → ftp/pathio.py

## Import Cycles
- None detected.

## Communities (155 total, 20 thin omitted)

### Community 0 - "feed_ftp.py"
Cohesion: 0.07
Nodes (78): Lock, _FakeResponse, test_cleanup_removes_only_stale_incomplete_groups(), test_cleanup_stale_downloads_removes_immediate_if_media_exists(), test_disk_capacity_waits_until_download_preserves_ten_percent(), test_episode_identity_accepts_dotted_separator(), test_extract_media_year(), test_get_best_staging_root_falls_back_when_fastest_disk_full() (+70 more)

### Community 1 - "asyncio"
Cohesion: 0.06
Nodes (41): Upload a single part with bot rotation on FloodWait. Instead of being pinned to…, Pre-read file chunks into an async queue ahead of upload workers. This overlaps…, _readahead_producer(), upload_part_with_retries(), FakeFloodWait, FakeRPCError, _make_bot(), _make_sent_msg() (+33 more)

### Community 2 - "ControlPlane"
Cohesion: 0.06
Nodes (23): ConflictError, ControlPlane, FeederSupervisor, _normalize_permissions(), Any, Path, Queue, _resolve_allowed() (+15 more)

### Community 3 - "accounts_manager.py"
Cohesion: 0.09
Nodes (35): addUser(), changeUserPassword(), cli_delete_user(), cli_list_users(), cli_migrate_passwords(), cli_set_password(), editPermissions(), get_db() (+27 more)

### Community 4 - "Remover arquivo de instalação"
Cohesion: 0.04
Nodes (46): 1. Build da imagem Docker, 1. "Connection refused" ao conectar no FTP, 1. Copiar o exemplo, 1. Criar o arquivo, 1. Explorar Recursos, 2. Copiar e colar o conteúdo abaixo, 2. Documentação Adicional, 2. Editar com seus dados (+38 more)

### Community 5 - "test_control_plane_p1.py"
Cohesion: 0.07
Nodes (19): generate_strm_files(), remove_stale_strm_files(), safe_windows_name(), stream_endpoint(), _append(), Cursor, FakeProcess, Feeder (+11 more)

### Community 6 - "🪟 Windows (Nativo)"
Cohesion: 0.05
Nodes (40): 10. Iniciar, 1. Instalar Dependências, 1. Instalar Homebrew, 1. Instalar Python, 2. Instalar Dependências, 2. Instalar MongoDB, 2. Instalar MongoDB (Local), 3. Clonar Repositório (+32 more)

### Community 7 - "RuntimeError"
Cohesion: 0.09
Nodes (34): RuntimeError, candidate_actions(), ensure_directory(), main(), Any, Aplica de forma reversivel correcoes confirmadas por metadados internos. Dry-…, relative_audit_parent(), unique_name() (+26 more)

### Community 8 - "test_audit_fixes.py"
Cohesion: 0.07
Nodes (18): BoundedLRUCache, Tiny LRUCache so we don't pull in cachetools., OrderedDict, _load(), asyncio, parametrize, Minimal pytest suite covering security-critical helpers., Import `ftp.<mod_name>` directly from file path, bypassing `ftp/__init__.py` so… (+10 more)

### Community 10 - "build_content_metadata_fix_plan.py"
Cohesion: 0.16
Nodes (19): defaultdict, Connection, Container, actual_extension(), bleach_candidate(), bleach_catalog(), bleach_targets(), clean_release_prefix() (+11 more)

### Community 11 - "ConnectionConditions"
Cohesion: 0.14
Nodes (3): ConnectionConditions, PathPermissions, test_absolute_client_paths_are_scoped_to_user_home()

### Community 12 - "validar_midias_ia.py"
Cohesion: 0.17
Nodes (23): apply_realignments(), _build_movie_result(), _build_series_result(), clean_ai_response(), clean_release_prefix(), encode_image_to_base64(), ensure_parent_dirs(), extract_title_from_filename() (+15 more)

### Community 13 - "Writer"
Cohesion: 0.17
Nodes (9): body_after_headers(), Files, Mongo, asyncio, parametrize, test_head_error_does_not_write_body(), test_non_loopback_stream_requires_bearer(), test_unsatisfiable_range_is_416_and_filename_cannot_inject_header() (+1 more)

### Community 14 - "server.py"
Cohesion: 0.16
Nodes (9): setlocale(), wrap_with_container(), AIOFTPException, NoAvailablePort, PathIOError, Exception, AbstractUserManager, MongoDBUserManager (+1 more)

### Community 15 - "tg.py"
Cohesion: 0.20
Nodes (12): File, get_file_limit(), get_media_session(), install_reliable_upload(), Upload bounded Telegram batches and propagate transport failures., sequential_save_file(), asyncio, test_reliable_upload_replaces_effective_pyrogram_method() (+4 more)

### Community 16 - "main.py"
Cohesion: 0.16
Nodes (14): send_document_bot_api(), extract_media_year(), get_media_category_priority(), get_required_config(), get_transcode_semaphore(), get_upload_semaphore(), is_loopback_host(), Wait for capacity, reserve the bot, and release it before retries/backoff. (+6 more)

### Community 17 - "clean_already_sent.py"
Cohesion: 0.17
Nodes (12): clean_sources(), force_remove_tree(), get_completed_telegram_items(), main(), normalize_string(), Run a single cleanup cycle., Attempt to forcibly remove a directory tree, handling read-only files…, Normalize string for safe comparison (ignore accents, symbols, case). (+4 more)

### Community 18 - "pathio.py"
Cohesion: 0.18
Nodes (12): AbstractPathIO, _ConnectionFailure, _DuplicateKeyError, movie_folder_score(), movie_tokens(), Exception, _PyMongoError, Reserve the first idle bot without interrupting active work. (+4 more)

### Community 20 - "upload_worker_parallel"
Cohesion: 0.16
Nodes (13): build_part_caption(), classify_media_type(), _cleanup_empty_parent_dirs(), get_contiguous_uploaded_parts(), is_staging_path(), Metrics, Upload worker with bot rotation and read-ahead. Each worker now receives the…, Classifica deterministicamente o tipo da mídia: 'SERIE', 'PORNO' ou 'FILME'. (+5 more)

### Community 21 - "audit_all_media_ffprobe.py"
Cohesion: 0.29
Nodes (12): load_cached(), main(), mounted_path(), normalize(), probe_one(), Any, Path, Auditoria incremental de toda a biblioteca NebulaFTP com ffprobe. O script e… (+4 more)

### Community 22 - "strm_downloader.py"
Cohesion: 0.22
Nodes (13): ArgumentParser, Namespace, build_parser(), DownloaderStats, get_best_staging_root(), get_configured_staging_dirs(), get_free_bytes(), main() (+5 more)

### Community 23 - "README.md"
Cohesion: 0.16
Nodes (10): Executar instalador, 🌟 Agradecimentos, Como ajudar:, ⚙️ Configuração (.env), 🤝 Contribuindo, 📊 Estatísticas, 📜 Licença, 🎯 O que é o Nebula FTP? (+2 more)

### Community 24 - "test_check_deps.py"
Cohesion: 0.14
Nodes (7): _load_local(), Coverage for tools.check_deps. The module is pure-stdlib (`importlib`,…, Roda o `python tools/check_deps.py` num venv limpo e verifica exit-code 0…, Import `tools/<mod_name>.py` directly from disc., Força uma 'falta' usando um pacote inexistente, verifica que o subprocess é…, test_check_deps_cli_exits_zero_when_satisfied(), test_ensure_runtime_dependencies_recovers_from_missing()

### Community 25 - "process_strm_item"
Cohesion: 0.18
Nodes (13): test_process_strm_item_moves_ready_media_to_stage(), guess_media_extension(), MediaValidator, process_strm_item(), prune_completed_strm_files(), Remove arquivos locais cujas mídias já foram concluídas no Telegram., Processa um único arquivo de mídia (seja .strm ou mídia pronta local) por vez:…, Lê a URL de stream contida dentro do arquivo .strm. (+5 more)

### Community 27 - "main"
Cohesion: 0.17
Nodes (13): cleanup_strm_duplicate_records(), folder_watcher(), garbage_collector(), get_upload_worker_count(), main(), Periodically clean up stale upload records and orphaned staging files., Watch staging directories for new files and enqueue them for upload. Delegates…, Ensure required MongoDB indexes exist. Skips indexes already present. (+5 more)

### Community 28 - "test_strm_downloader.py"
Cohesion: 0.17
Nodes (7): _FakeHTTPResponse, test_delete_strm_and_empty_parents(), test_delete_strm_preserves_folder_with_other_media(), test_failure_tracker_record_and_skip(), test_media_validator_completed_and_active(), delete_strm_and_empty_parents(), Deleta o arquivo .strm ou mídia de origem e, em seguida, remove recursivamente…

### Community 29 - "destination_for"
Cohesion: 0.17
Nodes (12): test_destination_and_mongo_parent_mapping(), test_register_in_nebula_queue(), destination_for(), ensure_mongo_parent_structure(), mongo_parent_for(), Gera o caminho estruturado para séries a partir do nome do arquivo., Calcula o caminho final esperado na estrutura do Nebula., Calcula o caminho do parent no MongoDB (ex: /raphael/Filmes/NomeDoFilme). (+4 more)

### Community 30 - "ECOSYSTEM.md"
Cohesion: 0.18
Nodes (10): Casos de Uso, <img src="https://github.com/samucamg/NebulaFTP/blob/master/img/logo_nebula_stream.png" alt="Logo Nebula Streaming" width="250px">, <img src="https://github.com/samucamg/NebulaFTP/blob/master/img/logo_nebula_webdav.png" alt="Logo Nebula Webdav" width="250px">, Integração com Banco de Dados, 🎬 NebulaStreaming, 🗂️ NebulaWebDAV, Recursos Principais, Recursos Principais (+2 more)

### Community 31 - "📱 Configuração do Telegram"
Cohesion: 0.18
Nodes (11): 3.1 Criar Novo Canal, 3.2 Adicionar os Bots como Admin, 🔧 Configurar o .env, 📱 Configuração do Telegram, Método 1: UseInfoBot (Mais Fácil), Método 2: Via Script Python, 🎯 O Que Você Precisa, 📢 Passo 3: Criar Canal (+3 more)

### Community 34 - "resolve_media_parent"
Cohesion: 0.20
Nodes (11): doc_download_timestamp(), log_queue_state(), queued_mongo_scanner(), Verifica se o local_path existe; se nao existir, tenta localiza-lo em qualquer…, Scans MongoDB for 'queued' files with local_path in oldest-downloaded-first…, Monitora pastas de stage (STAGING_DIRS) e registra arquivos pendentes/orfãos no…, resolve_local_path(), resolve_media_parent() (+3 more)

### Community 35 - "iter_strm_files_prioritized"
Cohesion: 0.18
Nodes (11): test_extract_media_year(), test_iter_strm_files_prioritized(), test_iter_strm_files_prioritized_includes_ready_media_files(), extract_media_year(), get_category_from_path(), is_incomplete_filename(), iter_strm_files_prioritized(), Extrai o ano da mídia (1900-2099) a partir do nome do arquivo e/ou pasta. (+3 more)

### Community 36 - "episode_identity"
Cohesion: 0.25
Nodes (9): test_movie_and_episode_identity(), episode_identity(), movie_identity(), normalize_media_title(), Normaliza título removendo acentos, pontuações e caixa alta para comparação…, Retorna a tupla (titulo_normalizado, ano) para filmes., Retorna a tupla (nome_serie_normalizado, temporada, episodio)., Atualiza os índices de mídias concluídas e em andamento do MongoDB. (+1 more)

### Community 37 - "<img src="https://github.com/samucamg/NebulaFTP/blob/master/img/logo_nebula_sftp.png" alt="Logo Nebula SFTP" width="250px">"
Cohesion: 0.20
Nodes (10): 👨‍💻 Autor, 📊 Comparativo de Versões, Comunidade (Gratuito), 🌟 Contribua, 📊 Estatísticas, <img src="https://github.com/samucamg/NebulaFTP/blob/master/img/logo_nebula_sftp.png" alt="Logo Nebula SFTP" width="250px">, NebulaFTP: Community vs Pro, NebulaStreaming / WebDAV / SFTP (+2 more)

### Community 38 - "Permission"
Cohesion: 0.27
Nodes (3): Permission, Constant-time verify. Accepts legacy plaintext for backwards compat., User

### Community 39 - "restore_mongo_from_telegram.py"
Cohesion: 0.36
Nodes (9): build_nodes_for_item(), categorize_path(), is_hash_name(), main(), normalize_str(), Scan message range with staggered start and rate limiting., restore_mongo_documents(), scan_bot_chunk() (+1 more)

### Community 40 - "Path"
Cohesion: 0.36
Nodes (3): FailureTracker, Path, Rastreia falhas de download (.strm com links expirados, 401, 404 ou erros) para…

### Community 41 - "final_all_fix.py"
Cohesion: 0.31
Nodes (5): apply_correction(), clean_title(), ensure_path(), quarantine_existing(), Remove release groups - multi-word first, then single

### Community 42 - "final_careful_fix.py"
Cohesion: 0.31
Nodes (5): apply_correction(), clean_title(), ensure_path(), quarantine_existing(), Clean title - only remove known release groups as whole words

### Community 43 - "fix_cosmetic_issues.py"
Cohesion: 0.31
Nodes (7): apply_correction(), ensure_path(), fix_double_parens(), fix_missing_words(), quarantine_existing(), Fix double parentheses like ((2002)) -> (2002), Fix cases like 'I Sam' -> 'I Am Sam', 'Talk Me' -> 'Talk to Me

### Community 44 - "handle_http_client"
Cohesion: 0.36
Nodes (8): Node, handle_http_client(), http_headers(), http_index(), http_player(), http_write_json(), list_completed_files(), stream_completed_file()

### Community 45 - "fix_mismatched_media_to_true_content.py"
Cohesion: 0.42
Nodes (8): clean_release_prefix(), ensure_parent_dirs(), get_actual_ext(), identify_true_content(), main(), normalized(), safe_component(), smart_title()

### Community 46 - "multi_frame_visual_ai_probe.py"
Cohesion: 0.36
Nodes (8): clean_ai_response(), extract_frame_at(), main(), process_media_until_recognized(), Path, query_local_ai_with_context(), multi_frame_visual_ai_probe.py Extração visual profunda multi-frame: Tira fotos…, safe_component()

### Community 47 - "test_realign_stream_titles.py"
Cohesion: 0.42
Nodes (8): clean_release_prefix(), ensure_parent_dirs(), extract_true_identity(), get_actual_ext(), main(), normalized(), safe_component(), smart_title()

### Community 48 - "comprehensive_fix_v2.py"
Cohesion: 0.36
Nodes (3): apply_correction(), ensure_path(), quarantine_existing()

### Community 49 - "fix_remaining_garbage.py"
Cohesion: 0.36
Nodes (5): apply_correction(), clean_title(), ensure_path(), quarantine_existing(), Remove release groups - now handles multi-word groups

### Community 50 - "fix_remaining_no_year.py"
Cohesion: 0.36
Nodes (3): apply_correction(), ensure_path(), quarantine_existing()

### Community 51 - "get_cache_dir"
Cohesion: 0.29
Nodes (6): get_cache_dir(), get_free_bytes(), Retorna os bytes livres no disco correspondente ao caminho., Retorna o diretorio de staging por ordem de prioridade/velocidade com espaco…, test_pathio_get_cache_dir_falls_back_when_fastest_disk_full(), test_pathio_get_cache_dir_prioritizes_fastest_disk_with_available_space()

### Community 52 - "audit_and_fix_all_drive_n.py"
Cohesion: 0.46
Nodes (7): clean_release_prefix(), ensure_parent_dirs(), get_actual_ext(), main(), normalized(), safe_component(), smart_title()

### Community 53 - "direct_probe_porno_and_ai_realign.py"
Cohesion: 0.46
Nodes (7): clean_release_prefix(), ensure_parent_dirs(), main(), probe_and_realign_doc(), query_local_ai(), safe_component(), smart_title()

### Community 54 - "fast_parallel_restore.py"
Cohesion: 0.54
Nodes (7): build_nodes_for_item(), get_category(), get_drive(), is_hash_name(), main(), normalize_path(), scan_bot_chunk()

### Community 55 - "fix_titles_clean_rebuild.py"
Cohesion: 0.32
Nodes (5): get_category(), is_hash_name(), normalize_path(), parse_parent_and_file(), fix_titles_clean_rebuild.py Abordagem definitiva: 1. Guardar todos os dados de…

### Community 56 - "fix_titles_final.py"
Cohesion: 0.32
Nodes (6): get_category(), is_hash_name(), normalize_path(), parse_parent_and_file(), fix_titles_final.py — Reconstrução definitiva e correta. FATO DESCOBERTO: - D:…, # IMPORTANT: We still have the full set in MongoDB sorted by tg_message from

### Community 57 - "fix_titles_precise.py"
Cohesion: 0.32
Nodes (6): get_category(), is_hash_name(), normalize_path(), parse_parent_and_file(), fix_titles_precise.py Reconstrói o mapeamento correto entre títulos (state…, Parse D:\\Filmes\\Movie Title (2020)\\Movie Title (2020).mkv Return…

### Community 58 - "full_manual_stream_repair.py"
Cohesion: 0.46
Nodes (7): clean_release_prefix(), ensure_parent_dirs(), get_actual_ext(), main(), normalized(), safe_component(), smart_title()

### Community 59 - "identify_anime_scenes.py"
Cohesion: 0.50
Nodes (7): compact_result(), identify_frame(), load_cache(), main(), Any, Path, Extrai um quadro pelo HTTP local do Nebula e identifica cenas de anime. Somente…

### Community 60 - "🤖 Passo 2: Criar Bot(s)"
Cohesion: 0.29
Nodes (7): 2.1 Abra o BotFather, 2.2 Crie um Novo Bot, 2.3 Escolha um Nome, 2.4 Escolha um Username, 2.5 Copie o Token, 2.6 (Opcional) Criar Mais Bots, 🤖 Passo 2: Criar Bot(s)

### Community 61 - "final_cleanup.py"
Cohesion: 0.43
Nodes (3): apply_correction(), ensure_path(), quarantine_existing()

### Community 62 - "final_cosmetic_cleanup.py"
Cohesion: 0.43
Nodes (5): apply_correction(), clean_filename(), ensure_path(), quarantine_existing(), Clean filename - remove quality info, release groups, etc. Keep year if present.

### Community 63 - "final_garbage_cleanup.py"
Cohesion: 0.43
Nodes (5): apply_correction(), clean_filename(), ensure_path(), quarantine_existing(), Clean filename base (without extension)

### Community 64 - "final_quarantine.py"
Cohesion: 0.43
Nodes (3): apply_correction(), ensure_path(), quarantine_existing()

### Community 65 - "CompactingFileHandler"
Cohesion: 0.33
Nodes (3): CompactingFileHandler, SafeStreamHandler, RotatingFileHandler

### Community 66 - "extensive_deep_clean_and_ai_realign.py"
Cohesion: 0.57
Nodes (6): clean_release_prefix(), ensure_parent_dirs(), main(), purge_temp_and_empty_dirs(), safe_component(), smart_title()

### Community 67 - "fix_all_title_content_mismatches.py"
Cohesion: 0.33
Nodes (5): classify_category(), get_folder_and_file(), normalize_path(), fix_all_title_content_mismatches.py O problema raiz: o…, Given original state path like D:\\Filmes\\Movie Title (2020)\\Movie Title…

### Community 68 - "full_scan_and_restore.py"
Cohesion: 0.62
Nodes (6): build_nodes_for_item(), get_category(), get_drive(), is_hash_name(), normalize_path(), scan_and_restore()

### Community 69 - "realign_all_media_to_true_stream_content.py"
Cohesion: 0.52
Nodes (6): clean_release_prefix(), ensure_parent_dirs(), main(), normalized(), safe_component(), smart_title()

### Community 70 - "verify_and_fix_final.py"
Cohesion: 0.43
Nodes (5): apply_correction(), clean_filename(), ensure_path(), quarantine_existing(), Clean filename - remove quality info, release groups, etc.

### Community 72 - "NebulaFTP — Security & Audit Notes"
Cohesion: 0.33
Nodes (5): Audit Outcome, How to enable FTPS, How to migrate legacy plaintext passwords, NebulaFTP — Security & Audit Notes, Reporting regressions

### Community 73 - "📋 Passo 1: Obter API Credentials"
Cohesion: 0.33
Nodes (6): 1.1 Acesse my.telegram.org, 1.2 Faça Login, 1.3 Confirme o Código, 1.4 Crie um App, 1.5 Copie as Credenciais, 📋 Passo 1: Obter API Credentials

### Community 77 - "fix_kaiji_series.py"
Cohesion: 0.53
Nodes (3): apply_correction(), ensure_path(), quarantine_existing()

### Community 78 - "fix_last_dirs.py"
Cohesion: 0.53
Nodes (3): apply_correction(), ensure_path(), quarantine_existing()

### Community 79 - "fix_last_two.py"
Cohesion: 0.53
Nodes (3): apply_correction(), ensure_path(), quarantine_existing()

### Community 81 - "audit_embedded_media_titles.py"
Cohesion: 0.67
Nodes (5): inspect_one(), main(), mounted_path(), Path, stratified_sample()

### Community 82 - "fix_database_categories.py"
Cohesion: 0.60
Nodes (5): build_nodes_for_item(), categorize_path(), is_hash_name(), normalize_str(), run()

### Community 83 - "purge_and_verify_clean_library.py"
Cohesion: 0.53
Nodes (5): categorize_filename(), is_hash_name(), is_truncated_file(), A multi-part file where the last chunk is exactly 64MB is truncated., verify_and_clean_library()

### Community 84 - "realign_library_with_local_ai.py"
Cohesion: 0.60
Nodes (5): ensure_parent_dirs(), main(), parse_ai_suggestion(), query_local_ai(), safe_component()

### Community 85 - "realign_porno_to_true_movies.py"
Cohesion: 0.60
Nodes (5): clean_release_prefix(), ensure_parent_dirs(), main(), safe_component(), smart_title()

### Community 86 - "realign_with_lm_studio_ai.py"
Cohesion: 0.60
Nodes (5): ensure_parent_dirs(), main(), parse_ai_json_or_text(), process_one(), safe_component()

### Community 87 - "restore_fast_single_bot.py"
Cohesion: 0.60
Nodes (5): build_nodes_for_item(), categorize_path(), is_hash_name(), main(), normalize_str()

### Community 88 - "🚀 NebulaFTP"
Cohesion: 0.40
Nodes (5): Características Stand-Alone, <img src="https://github.com/samucamg/NebulaFTP/blob/master/img/logo_nebula_ftp.png" alt="Logo Nebula FTP" width="250px">, 🚀 NebulaFTP, Repositório, Versões Disponíveis

### Community 89 - "🗺️ Roadmap"
Cohesion: 0.40
Nodes (5): ✅ Concluído, 🚧 Em Desenvolvimento (Q1 2026), 💡 Futuro (Q3-Q4 2026), 📅 Planejado (Q2 2026), 🗺️ Roadmap

### Community 90 - "<img src="https://raw.githubusercontent.com/samucamg/NebulaFTP/refs/heads/master/img/logo_nebula_cloud.png" alt="Logo Nebula FTP" width="300px">"
Cohesion: 0.40
Nodes (5): <img src="https://raw.githubusercontent.com/samucamg/NebulaFTP/refs/heads/master/img/logo_nebula_cloud.png" alt="Logo Nebula FTP" width="300px">, Por que Nebula?, 📦 Produtos do Ecossistema, 🌟 Visão Geral, 📖 Índice

### Community 91 - "🚀 Instalação"
Cohesion: 0.40
Nodes (5): 🚀 Instalação, NebulaFTP Community, NebulaFTP Pro / Outros Produtos, Via Docker (Recomendado), Via Python

### Community 92 - "final_fixes.py"
Cohesion: 0.70
Nodes (3): apply_correction(), ensure_path(), quarantine_existing()

### Community 93 - "parse_range"
Cohesion: 0.40
Nodes (4): parse_range(), HTTP `Range:` header parser for the inline streaming endpoint. Pure stdlib, no…, parse_range(), Re-export shim. Canonical implementation lives in ``ftp.range``. Kept here…

### Community 94 - "download_strm_multipart"
Cohesion: 0.40
Nodes (5): test_download_strm_multipart_direct_and_resume(), download_strm_multipart(), Bloqueia a execução até que haja espaço suficiente em disco., Realiza o download de uma URL com suporte a Range requests em múltiplas partes,…, wait_for_disk_capacity()

### Community 95 - "ai_realign_porno_frames.py"
Cohesion: 0.70
Nodes (4): ensure_parent_dirs(), main(), query_local_ai_for_porno_doc(), safe_component()

### Community 97 - "🚀 Guia de Instalação: NebulaFTP Community (Docker)"
Cohesion: 0.50
Nodes (4): 1. Instalar Docker + Docker Compose, 🚀 Guia de Instalação: NebulaFTP Community (Docker), 🛠️ Passo 1: Preparando o Servidor (Ubuntu 22.04), 📋 Pré-requisitos

### Community 98 - "🏗️ Arquitetura e Integração"
Cohesion: 0.50
Nodes (4): 🏗️ Arquitetura e Integração, Arquitetura Integrada (Streaming + WebDAV + SFTP), Arquitetura Stand-Alone (NebulaFTP), Fluxo de Integração

### Community 99 - "🔐 NebulaSFTP"
Cohesion: 0.50
Nodes (4): Casos de Uso, 🔐 NebulaSFTP, Recursos Principais, Status

### Community 100 - "💼 Casos de Uso"
Cohesion: 0.50
Nodes (4): 💼 Casos de Uso, Para Desenvolvedores, Para Empresas/Freelancers, Para Uso Pessoal

### Community 101 - "❓ Problemas Comuns"
Cohesion: 0.50
Nodes (4): "Chat not found", "Peer id invalid", ❓ Problemas Comuns, "The user must be an administrator"

### Community 104 - "💬 Suporte"
Cohesion: 0.50
Nodes (4): 🐛 Bugs e Sugestões, 💬 Comunidade, 📧 Contato Direto, 💬 Suporte

### Community 105 - "🎯 Casos de Uso"
Cohesion: 0.50
Nodes (4): 🎯 Casos de Uso, 🎓 Educacional, 🏠 Uso Pessoal, 🏢 Uso Profissional

### Community 106 - "detect_title_content_mismatches.py"
Cohesion: 0.83
Nodes (3): is_matching(), main(), normalize()

### Community 107 - "fast_local_ai_realign.py"
Cohesion: 0.83
Nodes (3): main(), process_one_item(), safe_component()

### Community 108 - "normalize_library_mongo.py"
Cohesion: 0.83
Nodes (3): clean_title(), normalized_movie_name(), run_mongo_normalization()

### Community 109 - "normalize_library_mongo_enhanced.py"
Cohesion: 0.83
Nodes (3): clean_title(), normalized_movie_name(), run_mongo_normalization()

### Community 111 - "image_to_base64"
Cohesion: 0.83
Nodes (3): image_to_base64(), main(), Path

### Community 112 - "📄 Licença"
Cohesion: 0.67
Nodes (3): 📄 Licença, NebulaFTP Community, NebulaFTP Pro / Streaming / WebDAV / SFTP

### Community 113 - "🛠️ Requisitos"
Cohesion: 0.67
Nodes (3): NebulaFTP Community, NebulaFTP Pro / Streaming / WebDAV / SFTP, 🛠️ Requisitos

### Community 116 - "🔧 Recursos Técnicos"
Cohesion: 0.67
Nodes (3): Arquitetura, 🔧 Recursos Técnicos, Tecnologias

### Community 117 - "📊 Demonstração"
Cohesion: 0.67
Nodes (3): 📊 Demonstração, Screenshots, Upload Turbo (Staging Local)

### Community 118 - "📖 Documentação"
Cohesion: 0.67
Nodes (3): 📖 Documentação, 🏗️ Ecossistema Nebula, 🎓 Para Iniciantes

### Community 119 - "🚀 Início Rápido"
Cohesion: 0.67
Nodes (3): 🚀 Início Rápido, Opção 1: Docker (Recomendado) 🐳, Opção 2: Python Direto 🐍

## Knowledge Gaps
- **160 isolated node(s):** `**Transforme o Telegram em seu Armazenamento Ilimitado**`, `🎯 O que é o Nebula FTP?`, `Upload Turbo (Staging Local)`, `Screenshots`, `🎥 Vídeo Tutorial` (+155 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **20 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `upload_part_with_retries()` connect `asyncio` to `main.py`, `RuntimeError`?**
  _High betweenness centrality (0.041) - this node is a cross-community bridge._
- **Why does `ControlPlane` connect `ControlPlane` to `accounts_manager.py`, `test_control_plane_p1.py`, `Writer`, `main.py`, `MongoDBPathIO`, `main`?**
  _High betweenness centrality (0.035) - this node is a cross-community bridge._
- **Why does `hash_password()` connect `accounts_manager.py` to `ControlPlane`, `server.py`, `RuntimeError`?**
  _High betweenness centrality (0.027) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `Server` (e.g. with `StreamIO` and `PathIOError`) actually correct?**
  _`Server` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `ControlPlane` (e.g. with `MongoDBPathIO` and `test_canonical_p1_routes()`) actually correct?**
  _`ControlPlane` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `MongoDBPathIO` (e.g. with `ControlPlane` and `PathIOError`) actually correct?**
  _`MongoDBPathIO` has 4 INFERRED edges - model-reasoned connections that need verification._
- **What connects `**Transforme o Telegram em seu Armazenamento Ilimitado**`, `🎯 O que é o Nebula FTP?`, `Upload Turbo (Staging Local)` to the rest of the system?**
  _160 weakly-connected nodes found - possible documentation gaps or missing edges._