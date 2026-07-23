"""Runtime dependency guard for NebulaFTP.

Salvamos o fast-fail do arranque (`ImportError: pyrogram`, `bcrypt`, etc.)
garantindo que as bibliotecas listadas em `requirements.txt` estejam
presentes no interpretador antes de qualquer parte do servidor ser
tocada.

Estratégia:

1. Para cada módulo do `RUNTIME_REQUIRED`, tenta ``importlib.util.find_spec``
   e ``importlib.import_module`` (este último em subprocess explícito
   quando há risco conhecido de side-effect top-level, ex.: ``pyrogram``
   que chama ``asyncio.get_event_loop()`` em Py 3.14 e rebenta).
2. Se o pacote está ausente ou o import falha, despacha um subprocess
   ``sys.executable -m pip install --quiet --disable-pip-version-check``
   para a faixa de versões de ``requirements.txt``.
3. Re-importa. Se ainda falhar, aborta com mensagem clara (em vez do
   ``Traceback`` enigmático que aparecia antes).

Esta função é chamada no topo de ``main.main()`` **antes** de qualquer
import runtime ser executado. Importá-la não dispara side-effect algum
em libs de runtime — é Python stdlib + ``subprocess`` puro.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable

# Cada item: nome do módulo teste, nome do pacote pip (geralmente o mesmo).
# Comentários marcam libs que disparam side-effects top-level sensíveis a
# Python 3.14 (sem event loop no import-time).
RUNTIME_REQUIRED: tuple[tuple[str, str], ...] = (
    ("bcrypt", "bcrypt"),
    ("cryptography", "cryptography"),
    ("dns.resolver", "dnspython"),
    ("dotenv", "python-dotenv"),
    ("aiofiles", "aiofiles"),
    ("pymongo", "pymongo"),
    # ``motor`` exige pymongo, importa top-level sem side-effects.
    ("motor", "motor"),
    # ``aiohttp`` top-level é seguro.
    ("aiohttp", "aiohttp"),
    # ``pyrogram`` em Py 3.14 falha em ``asyncio.get_event_loop`` no
    # import-time. Importamos em subprocess para detetar, mas o
    # ``main.py`` continua a ter o patch ``asyncio.set_event_loop`` que
    # torna o uso real seguro.
    ("pyrogram", "pyrogram"),
    ("tgcrypto", "tgcrypto"),
    ("pyaes", "pyaes"),
    # PySocks distribui como ``pysocks`` mas expõe ``import socks``;
    # alinhamos o módulo real aqui, e mantemos o nome pip em lowercase
    # para casar com `_read_pinned_specs`.
    ("socks", "pysocks"),
    ("pyftpdlib", "pyftpdlib"),
)


def _read_pinned_specs(path: str = "requirements.txt") -> dict[str, str]:
    """Extrai ``pacote>=X,<Y`` por nome do requirements.txt.

    Comentários (``#``) e linhas em branco são ignorados. Aceita múltiplas
    flags (e.g. ``foo>=1,<2,!=1.4``) e devolve a string completa para o
    pip como spec.
    """
    if not os.path.exists(path):
        return {}
    out: dict[str, str] = {}
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        match = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", line)
        if not match:
            continue
        name, spec = match.group(1), match.group(2).strip()
        if spec:
            out[name.lower()] = spec
    return out


def _probe(module_name: str) -> tuple[bool, str | None]:
    """Deteta se ``module_name`` está importável no interpretador atual.

    Retorna ``(ok, error_message)``. Não invoca ``import_module`` em
    pacotes que disparam side-effects pesados no carregamento: usa
    ``find_spec`` (sem executar) como deteção barata.
    """
    try:
        if importlib.util.find_spec(module_name) is None:
            return False, "find_spec returned None"
    except (ImportError, ValueError) as exc:
        return False, f"find_spec failed: {exc}"
    # Confirma import real (alguns meta-path finders mentem). Mas
    # saltamos o import real para pyrogram/motor em Py 3.14 porque
    # dispara RuntimeError; o find_spec é suficiente.
    if module_name in {"pyrogram"} and sys.version_info >= (3, 14):
        return True, None
    try:
        importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - queremos qualquer falha
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


def _pip_install(spec: str) -> int:
    """Despacha ``pip install`` num subprocess isolado.

    O subprocess garante que o nosso interpretador não fica bloqueado
    por outros ``pip`` pendentes e que o path de import é o mesmo do
    ``sys.executable``.
    """
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--disable-pip-version-check",
        *spec.split(),
    ]
    # ``spec`` é sempre construído a partir da nossa tabela interna
    # ``RUNTIME_REQUIRED``, não de input externo. O subprocess é seguro.
    proc = subprocess.run(  # noqa: S603 - input vindo de RUNTIME_REQUIRED
        cmd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"[deps] pip install falhou para {spec!r}\n"
            f"[deps]   stdout: {proc.stdout.strip()}\n"
            f"[deps]   stderr: {proc.stderr.strip()}\n"
        )
    return proc.returncode


def ensure_runtime_dependencies(
    required: Iterable[tuple[str, str]] = RUNTIME_REQUIRED,
    requirements_path: str = "requirements.txt",
) -> None:
    """Garante que cada pacote em ``required`` está instalado. Idempotente.

    Args:
        required: tupla de pares ``(modulo, pacote_pip)``.
        requirements_path: caminho para o ``requirements.txt`` cujas
            faixas de versão devem ser respeitadas na reinstalação.

    Levanta ``RuntimeError`` apenas quando o pacote não pode ser
    instalado mesmo após uma tentativa de ``pip install``. Em caso de
    interrupção do utilizador (``KeyboardInterrupt``), propaga.
    """
    pins = _read_pinned_specs(requirements_path)
    missing: list[tuple[str, str, str | None]] = []  # (module, pip_name, reason)

    for module_name, pip_name in required:
        ok, reason = _probe(module_name)
        if not ok:
            missing.append((module_name, pip_name, reason))

    if not missing:
        return

    sys.stderr.write(
        f"[deps] {len(missing)} dependencia(s) em falta; a tentar instalar...\n"
    )

    # Instala em batch (uma chamada pip) quando todos os specs existem
    # nos pins. Caso contrário, instala individualmente.
    batchspecs: list[str] = []
    for _module_name, pip_name, _reason in missing:
        spec = pins.get(pip_name.lower())
        candidate = f"{pip_name}{spec}" if spec else pip_name
        batchspecs.append(candidate)

    rc = _pip_install(" ".join(batchspecs))
    if rc != 0:
        # fallback: instala um-a-um para isolar qual pacote falhou
        for candidate in batchspecs:
            _pip_install(candidate)

    # Re-probe. Quem continuou a falhar após install, é fatal.
    still_missing = [(m, p) for (m, p, _r) in missing if not _probe(m)[0]]
    if still_missing:
        joined = ", ".join(f"{p} (módulo {m})" for m, p in still_missing)
        raise RuntimeError(
            f"Falha ao instalar dependências necessárias: {joined}. "
            "Execute `pip install -r requirements.txt` manualmente."
        )
    sys.stderr.write("[deps] todas as dependências verificadas.\n")


def main() -> int:  # pragma: no cover - ponto de entrada CLI
    """Permite correr manualmente: ``python -m tools.check_deps`` ou
    ``python tools/check_deps.py``."""
    try:
        ensure_runtime_dependencies()
    except RuntimeError as exc:
        sys.stderr.write(f"[deps] {exc}\n")
        return 1
    print(json.dumps({"ok": True, "modules": [m for m, _ in RUNTIME_REQUIRED]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
