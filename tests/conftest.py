"""Testes 100% offline: nada aqui toca rede, Jev ou CLOB."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_json():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text())

    return _load
