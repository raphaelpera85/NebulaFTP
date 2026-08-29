import shutil
from types import SimpleNamespace
import pytest
import ftp.pathio as pathio_mod


def test_pathio_get_cache_dir_prioritizes_fastest_disk_with_available_space(tmp_path, monkeypatch):
    disk_e_path = tmp_path / "E_NebulaStage"
    disk_f_path = tmp_path / "F_NebulaStage"
    disk_e_path.mkdir()
    disk_f_path.mkdir()
    disk_e = str(disk_e_path)
    disk_f = str(disk_f_path)

    # Ordem de velocidade: E (SSD mais rápido) primeiro, F segundo
    monkeypatch.setattr(pathio_mod, "CACHE_DIRS", [disk_e, disk_f])

    def fake_disk_usage(path):
        p_str = str(path)
        if "E_NebulaStage" in p_str:
            return SimpleNamespace(total=100 * 1024**3, free=60 * 1024**3)
        if "F_NebulaStage" in p_str:
            return SimpleNamespace(total=2000 * 1024**3, free=800 * 1024**3)
        return SimpleNamespace(total=1000, free=100)

    monkeypatch.setattr(pathio_mod.shutil, "disk_usage", fake_disk_usage)

    # Como Disk E é o mais rápido e possui 60 GB livres (> reserva), deve ser o escolhido
    best_dir = pathio_mod.get_cache_dir()
    assert best_dir == disk_e


def test_pathio_get_cache_dir_falls_back_when_fastest_disk_full(tmp_path, monkeypatch):
    disk_e_path = tmp_path / "E_NebulaStage"
    disk_f_path = tmp_path / "F_NebulaStage"
    disk_e_path.mkdir()
    disk_f_path.mkdir()
    disk_e = str(disk_e_path)
    disk_f = str(disk_f_path)

    monkeypatch.setattr(pathio_mod, "CACHE_DIRS", [disk_e, disk_f])

    def fake_disk_usage(path):
        p_str = str(path)
        if "E_NebulaStage" in p_str:
            # Disk E cheio (apenas 1 GB livre)
            return SimpleNamespace(total=100 * 1024**3, free=1 * 1024**3)
        if "F_NebulaStage" in p_str:
            # Disk F com 800 GB livres
            return SimpleNamespace(total=2000 * 1024**3, free=800 * 1024**3)
        return SimpleNamespace(total=1000, free=100)

    monkeypatch.setattr(pathio_mod.shutil, "disk_usage", fake_disk_usage)

    # Como Disk E está cheio, faz fallback para Disk F
    best_dir = pathio_mod.get_cache_dir()
    assert best_dir == disk_f
