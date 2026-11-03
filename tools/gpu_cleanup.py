"""Чистильщик видеокарты: снять настройки приложения и вернуть пресет Afterburner.

Запускается задачей `MinerGPUReset` (при входе в систему и раз в 5 минут), а также
вручную. Если приложение живо (маркер `data/gpu-owned.json` указывает на живой pid),
ничего не делает — иначе мешало бы ему.

    python.exe tools\\gpu_cleanup.py            # убрать, если приложение мертво
    python.exe tools\\gpu_cleanup.py --force    # убрать всегда (ручная уборка)
    python.exe tools\\gpu_cleanup.py --no-ab    # без перезапуска Afterburner
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minerapp import owner  # noqa: E402

if __name__ == "__main__":
    force = "--force" in sys.argv
    with_ab = "--no-ab" not in sys.argv
    print("состояние:", owner.why_stale())
    print(owner.cleanup(apply_ab=with_ab, force=force))
