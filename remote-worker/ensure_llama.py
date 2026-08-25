"""Ставит llama-cpp-python и проверяет llama_backend_init в отдельном процессе."""
from __future__ import annotations

import os
import subprocess
import sys

CPU = "https://abetlen.github.io/llama-cpp-python/whl/cpu"
ROOT = os.path.dirname(os.path.abspath(__file__))
TRIES = (
    "llama-cpp-python==0.3.2",
    "llama-cpp-python==0.3.8",
    "llama-cpp-python==0.3.18",
    "llama-cpp-python>=0.3.0",
)


def _pip(*args: str) -> bool:
    return subprocess.call([sys.executable, "-m", "pip", *args]) == 0


def _backend_ok() -> bool:
    root = ROOT.replace("\\", "\\\\")
    code = (
        "import os,sys;"
        "os.environ['CUDA_VISIBLE_DEVICES']='-1';"
        f"sys.path.insert(0, r'{root}');"
        "from server import _prepare_native_libs;"
        "_prepare_native_libs();"
        "from llama_cpp import llama_cpp;"
        "llama_cpp.llama_backend_init();"
        "print('backend OK')"
    )
    return subprocess.call([sys.executable, "-c", code]) == 0


def main() -> int:
    if _backend_ok():
        print("llama-cpp-python OK")
        return 0
    print("Tekuschaja sborka padaet na CPU (0xc000001d). Stavliu druguju...")
    for spec in TRIES:
        print("install", spec)
        _pip("uninstall", "-y", "llama-cpp-python")
        ok = _pip(
            "install",
            spec,
            "--force-reinstall",
            "--no-cache-dir",
            "--only-binary=llama-cpp-python",
            "--extra-index-url",
            CPU,
        )
        if ok and _backend_ok():
            print("llama-cpp-python OK")
            return 0
    print("CPU-sborka ne zapustilas (Windows Error 0xc000001d).")
    print("Eto instrukcii AVX, kotoryh net u processora.")
    print("Postav'te VC++ x64: https://aka.ms/vs/17/release/vc_redist.x64.exe")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
