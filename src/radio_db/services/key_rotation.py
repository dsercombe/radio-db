from __future__ import annotations

import json
from pathlib import Path


def next_key_index(provider: str, pool_size: int, state_path: Path | None = None) -> int:
    if pool_size <= 1:
        return 0
    path = state_path or (Path(".radio_db_state") / "key_rotation.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    current = int(data.get(provider, 0))
    idx = current % pool_size
    data[provider] = current + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return idx

