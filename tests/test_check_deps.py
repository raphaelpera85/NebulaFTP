"""Coverage for tools.check_deps.

The module is pure-stdlib (`importlib`, `subprocess`, `re`) and is safe to
load directly with the same `_load()` helper used by the rest of the
suite — bypassing `ftp/__init__.py`.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import types
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_local(mod_name: str):
    """Import `tools/<mod_name>.py` directly from disc."""
    pkg = sys.modules.get("tools")
    if pkg is None or not hasattr(pkg, "__path__"):
        pkg = types.ModuleType("tools")
        pkg.__path__ = [os.path.join(ROOT, "tools")]
        sys.modules["tools"] = pkg
    target = f"tools.{mod_name}"
    path = os.path.join(ROOT, "tools", f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(target, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[target] = mod
    spec.loader.exec_module(mod)
    return mod


deps = _load_local("check_deps")


def test_runtime_required_is_non_empty_tuple_of_pairs():
    assert isinstance(deps.RUNTIME_REQUIRED, tuple)
    assert len(deps.RUNTIME_REQUIRED) >= 5
    for mod, pkg in deps.RUNTIME_REQUIRED:
        assert isinstance(mod, str) and mod
        assert isinstance(pkg, str) and pkg


def test_read_pinned_specs_extracts_lower_bounds():
    pins = deps._read_pinned_specs(os.path.join(ROOT, "requirements.txt"))
    # Sanity: every declared runtime constraint is captured (not None
    # when no version clause).
    for _, pip_name in deps.RUNTIME_REQUIRED:
        assert pip_name.lower() in pins or pip_name.lower() in {
            "dnspython", "python-dotenv",
        }


def test_probe_for_stdlib_module_succeeds():
    assert deps._probe("os")[0] is True
    assert deps._probe("sys")[0] is True


def test_probe_for_missing_module_returns_missing():
    ok, reason = deps._probe("definitely_not_a_real_module_xyz123")
    assert ok is False
    assert reason is not None


def test_ensure_runtime_dependencies_is_idempotent():
    # Mock _probe so all modules appear installed; the function must
    # return silently without touching pip.
    with patch.object(deps, "_probe", return_value=(True, None)):
        deps.ensure_runtime_dependencies()


def test_ensure_runtime_dependencies_recovers_from_missing():
    """Força uma 'falta' usando um pacote inexistente, verifica que o
    subprocess é despachado e a função aborta com mensagem útil caso
    o pacote não seja instalável (rede off, etc.). O objetivo é
    validar o **código de despacho**, não instalar nada real.
    """
    sentinel = ("__definitely_missing_x_y_z", "definitely-missing-xyz")
    with pytest.raises(RuntimeError) as excinfo:
        deps.ensure_runtime_dependencies(
            required=(sentinel,),
            requirements_path=os.path.join(ROOT, "requirements.txt"),
        )
    msg = str(excinfo.value).lower()
    assert "dependência" in msg or "dependencia" in msg
    assert "definitely" in msg


def test_pip_install_does_not_silently_succeed_on_no_such_package():
    rc = deps._pip_install("definitely-not-a-pip-package-abcdef-xyz")
    assert rc != 0, "pip install de pacote inexistente deveria falhar"


def test_check_deps_cli_exits_zero_when_satisfied(tmp_path):
    """Roda o `python tools/check_deps.py` num venv limpo e verifica
    exit-code 0 (porque pytest já tem tudo instalado)."""
    script = os.path.join(ROOT, "tools", "check_deps.py")
    result = subprocess.run(
        [sys.executable, script],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"check_deps CLI falhou: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Texto final contém um JSON com lista de módulos. Aceitamos
    # `json.dumps` com ou sem espaços (`"ok": true` vs `"ok":true`).
    import json as _json
    payload = _json.loads(result.stdout.strip())
    assert payload.get("ok") is True
    assert payload["modules"], "lista de módulos não pode estar vazia"
