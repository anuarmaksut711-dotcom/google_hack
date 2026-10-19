"""Explicit local demo composition; never a production fallback."""
import os

from ..config import CLOUD_MARKERS

NOTICE = ("ДЕМО · только тестовые задания и календарь. Google, Firestore и Gemini не вызываются. "
          "Детерминированный исполнитель вместо Gemini; хранилище в памяти, перезапуск сбрасывает демо.")


def require_demo(enabled: bool) -> None:
    if enabled is not True or CLOUD_MARKERS.intersection(os.environ):
        raise ValueError("explicit_local_demo_required_no_cloud_runtime")
