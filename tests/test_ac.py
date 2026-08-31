"""Tests for conservative AC state handling."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ac = _load_module(
    "tapo_ir_ac",
    ROOT / "custom_components" / "tapo_ir" / "ac.py",
)


class AcTests(unittest.TestCase):
    """Never fabricate unknown fields in a physical AC command."""

    def test_parses_complete_status(self) -> None:
        self.assertEqual(
            ac.parse_ac_status({"ac_status": "P1_M0_T22_S1_D0"}),
            {"P": 1, "M": 0, "T": 22, "S": 1, "D": 0},
        )

    def test_builds_native_h110_payload(self) -> None:
        self.assertEqual(
            ac.build_ac_payload({"P": 1, "M": 0, "T": 22, "S": 1, "D": 0}),
            {
                "power": 1,
                "mode": 0,
                "temp": 22,
                "wind_speed": 1,
                "wind_direct": 0,
            },
        )

    def test_builds_payload_with_pressed_fid(self) -> None:
        self.assertEqual(
            ac.build_ac_payload(
                {"P": 1, "M": 0, "T": 22, "S": 1, "D": 0},
                pressed_fid=6,
            ),
            {
                "power": 1,
                "mode": 0,
                "temp": 22,
                "wind_speed": 1,
                "wind_direct": 0,
                "pressed_fid": 6,
            },
        )

    def test_builds_power_off_as_integer(self) -> None:
        self.assertEqual(
            ac.build_ac_payload({"P": 0, "M": 0, "T": 22, "S": 1, "D": 0})[
                "power"
            ],
            0,
        )

    def test_rejects_incomplete_state(self) -> None:
        with self.assertRaises(ac.AcStateError):
            ac.build_ac_payload({"P": 1, "M": 0, "T": 22})


if __name__ == "__main__":
    unittest.main()
