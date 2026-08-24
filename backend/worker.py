"""Совместимый запуск: python worker.py ... → remote-worker/server.py"""
from pathlib import Path
import runpy

_target = Path(__file__).resolve().parents[1] / "remote-worker" / "server.py"
runpy.run_path(str(_target), run_name="__main__")
