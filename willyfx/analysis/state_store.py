import json
import os
from typing import Any, Dict

DEFAULT_STATE_PATH = "main3_state.json"


def load_state(path: str = None) -> Dict[str, Any]:
    path = path or os.getenv("MAIN3_STATE_FILE", DEFAULT_STATE_PATH)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict[str, Any], path: str = None) -> bool:
    path = path or os.getenv("MAIN3_STATE_FILE", DEFAULT_STATE_PATH)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
