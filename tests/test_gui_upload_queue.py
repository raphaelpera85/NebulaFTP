from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gui import (
    parse_downloader_scan_progress,
    should_display_upload_card,
    upload_bot_indexes,
    upload_status_label,
)


def test_queued_upload_has_visible_waiting_status():
    assert upload_status_label({"status": "queued"}) == "Na fila — aguardando envio"


def test_staging_upload_is_not_presented_as_active_transfer():
    assert upload_status_label({"status": "staging"}) == "Preparado — aguardando worker de envio"


def test_missing_local_file_explains_recovery_state():
    label = upload_status_label(
        {
            "status": "queued",
            "recovery_required": True,
            "failed_reason": "local_path_missing",
        }
    )

    assert "Aguardando arquivo local" in label
    assert "recuperação necessária" in label


def test_uploading_state_includes_worker():
    assert upload_status_label({"status": "uploading", "worker_id": 7}) == "Uploading | Worker #7"


def test_upload_bot_indexes_ignores_malformed_part_entries():
    assert upload_bot_indexes([None, "bad", {}, {"bot_index": "2"}, {"bot_index": 1}]) == [2]


def test_upload_bot_indexes_handles_malformed_parts_container():
    assert upload_bot_indexes(None) == []
    assert upload_bot_indexes({"bot_index": 1}) == []


def test_downloader_scan_progress_log_is_parsed_for_the_status_panel():
    assert parse_downloader_scan_progress(
        "Varredura em andamento: pastas=120 arquivos_verificados=45000 "
        "candidatos=23000 lotes_processados=92"
    ) == (120, 45000, 23000, 92)


def test_unrelated_downloader_log_is_not_treated_as_scan_progress():
    assert parse_downloader_scan_progress("Escaneando .strm em lotes...") is None


def test_should_display_upload_card_only_for_active_or_recovery():
    # Uploading: active transfer -> card visible
    assert should_display_upload_card({"status": "uploading"}) is True
    # Queued with recovery required -> card visible
    assert should_display_upload_card({"status": "queued", "recovery_required": True}) is True
    # Staging with recovery required -> card visible
    assert should_display_upload_card({"status": "staging", "recovery_required": True}) is True
    # Normal queued without recovery -> card hidden (header counter only)
    assert should_display_upload_card({"status": "queued"}) is False
    # Normal staging without recovery -> card hidden (header counter only)
    assert should_display_upload_card({"status": "staging"}) is False
    assert should_display_upload_card({"status": "queued", "recovery_required": False}) is False
