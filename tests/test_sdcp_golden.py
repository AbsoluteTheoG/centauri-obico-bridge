"""The original Centauri Carbon (SDCP) behaviour must not change.

``tests/golden/sdcp_golden.json`` was recorded from the bridge before
multi-printer support existed; see ``tests/sdcp_harness.py``.
"""

import json
import pathlib

from tests import sdcp_harness

GOLDEN = pathlib.Path(__file__).parent / "golden" / "sdcp_golden.json"


def _normalise(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def test_sdcp_behaviour_matches_golden():
    expected = json.loads(GOLDEN.read_text())
    actual = _normalise(sdcp_harness.run())
    assert actual.keys() == expected.keys()
    for name in expected:
        assert actual[name] == expected[name], f"SDCP behaviour changed in scenario {name!r}"
