from __future__ import annotations

import unittest

from sts_bot.managed_probe import (
    ManagedBlockWriteResult,
    ManagedEnergyWriteResult,
    ManagedEnemySnapshot,
    ManagedGoldWriteResult,
    ManagedPowerAliasResult,
    ManagedPowerSnapshot,
    ManagedPowerWriteResult,
    ManagedProbeSnapshot,
)


class ManagedProbeSnapshotTest(unittest.TestCase):
    def test_from_payload_parses_numeric_summary_and_extras(self) -> None:
        snapshot = ManagedProbeSnapshot.from_payload(
            {
                "pid": 2348,
                "runtime_version": "9.0.4",
                "floor": 6,
                "ascension": 4,
                "gold": 453,
                "hp": 29,
                "max_hp": 80,
                "block": 5,
                "energy": 2,
                "max_energy": 3,
                "objects": {"player": "0x857de060"},
                "sources": {"gold": "NTopBarGold._currentGold"},
                "player_powers": [
                    {
                        "address": "0x82bf0000",
                        "type": "MegaCrit.Sts2.Core.Models.Powers.StrengthPower",
                        "amount": 2,
                    }
                ],
                "enemies": [
                    {
                        "address": "0x82be34a0",
                        "current_hp": 53,
                        "max_hp": 61,
                        "block": 0,
                        "powers": [
                            {
                                "address": "0x82bf0100",
                                "type": "MegaCrit.Sts2.Core.Models.Powers.VulnerablePower",
                                "amount": 1,
                            }
                        ],
                    }
                ],
            }
        )

        self.assertEqual(snapshot.pid, 2348)
        self.assertEqual(snapshot.floor, 6)
        self.assertEqual(snapshot.block, 5)
        self.assertEqual(snapshot.player_powers, [ManagedPowerSnapshot("0x82bf0000", "MegaCrit.Sts2.Core.Models.Powers.StrengthPower", 2)])
        self.assertEqual(
            snapshot.enemies,
            [
                ManagedEnemySnapshot(
                    "0x82be34a0",
                    53,
                    61,
                    0,
                    [ManagedPowerSnapshot("0x82bf0100", "MegaCrit.Sts2.Core.Models.Powers.VulnerablePower", 1)],
                )
            ],
        )
        self.assertEqual(snapshot.to_dict()["gold"], 453)

    def test_from_payload_defaults_missing_arrays_and_maps(self) -> None:
        snapshot = ManagedProbeSnapshot.from_payload(
            {
                "pid": 1,
                "runtime_version": "9.0.0",
                "floor": 1,
                "ascension": 0,
                "gold": 99,
                "hp": 80,
                "max_hp": 80,
                "block": 0,
                "energy": 3,
                "max_energy": 3,
            }
        )

        self.assertEqual(snapshot.objects, {})
        self.assertEqual(snapshot.sources, {})
        self.assertEqual(snapshot.player_powers, [])
        self.assertEqual(snapshot.enemies, [])

    def test_block_write_result_parses_write_metadata(self) -> None:
        result = ManagedBlockWriteResult.from_payload(
            {
                "pid": 2348,
                "runtime_version": "9.0.0",
                "floor": 6,
                "ascension": 4,
                "gold": 453,
                "hp": 22,
                "max_hp": 80,
                "block": 9,
                "energy": 0,
                "max_energy": 3,
                "write": {
                    "field": "player_block",
                    "address": "0x857de2ac",
                    "previous": 5,
                    "requested": 9,
                },
            }
        )

        self.assertEqual(result.field, "player_block")
        self.assertEqual(result.address, "0x857de2ac")
        self.assertEqual(result.previous, 5)
        self.assertEqual(result.requested, 9)
        self.assertEqual(result.snapshot.block, 9)

    def test_power_write_result_parses_write_metadata(self) -> None:
        result = ManagedPowerWriteResult.from_payload(
            {
                "pid": 2348,
                "runtime_version": "9.0.0",
                "floor": 6,
                "ascension": 4,
                "gold": 453,
                "hp": 22,
                "max_hp": 80,
                "block": 9,
                "energy": 0,
                "max_energy": 3,
                "enemies": [
                    {
                        "address": "0x82be34a0",
                        "current_hp": 30,
                        "max_hp": 61,
                        "block": 0,
                        "powers": [
                            {
                                "address": "0x8582b720",
                                "type": "MegaCrit.Sts2.Core.Models.Powers.VulnerablePower",
                                "amount": 4,
                            }
                        ],
                    }
                ],
                "write": {
                    "field": "power_amount",
                    "target": "enemy",
                    "power_type": "MegaCrit.Sts2.Core.Models.Powers.VulnerablePower",
                    "power_address": "0x8582b720",
                    "target_creature": "0x82be34a0",
                    "address": "0x8582b748",
                    "previous": 2,
                    "requested": 4,
                },
            }
        )

        self.assertEqual(result.target, "enemy")
        self.assertEqual(result.power_type, "MegaCrit.Sts2.Core.Models.Powers.VulnerablePower")
        self.assertEqual(result.power_address, "0x8582b720")
        self.assertEqual(result.previous, 2)
        self.assertEqual(result.requested, 4)

    def test_power_alias_result_parses_write_metadata(self) -> None:
        result = ManagedPowerAliasResult.from_payload(
            {
                "pid": 2348,
                "runtime_version": "9.0.0",
                "floor": 7,
                "ascension": 4,
                "gold": 460,
                "hp": 28,
                "max_hp": 80,
                "block": 0,
                "energy": 3,
                "max_energy": 3,
                "player_powers": [
                    {
                        "address": "0x8583cde0",
                        "type": "MegaCrit.Sts2.Core.Models.Powers.StrengthPower",
                        "amount": 10,
                    }
                ],
                "write": {
                    "field": "power_list_alias",
                    "source": "enemy",
                    "dest": "player",
                    "source_creature": "0x82bda2b0",
                    "dest_creature": "0x857de220",
                    "previous": "0x857de2d0",
                    "requested": "0x82bda360",
                    "address": "0x857de288",
                },
            }
        )

        self.assertEqual(result.field, "power_list_alias")
        self.assertEqual(result.source, "enemy")
        self.assertEqual(result.dest, "player")
        self.assertEqual(result.snapshot.player_powers[0].type_name, "MegaCrit.Sts2.Core.Models.Powers.StrengthPower")

    def test_energy_write_result_parses_write_metadata(self) -> None:
        result = ManagedEnergyWriteResult.from_payload(
            {
                "pid": 2348,
                "runtime_version": "9.0.0",
                "floor": 11,
                "ascension": 4,
                "gold": 538,
                "hp": 23,
                "max_hp": 80,
                "block": 0,
                "energy": 100,
                "max_energy": 100,
                "write": {
                    "field": "player_energy",
                    "energy_address": "0x84c2c508",
                    "previous_energy": 1,
                    "requested_energy": 100,
                    "max_energy_address": "0x857bbb40",
                    "previous_max_energy": 3,
                    "requested_max_energy": 100,
                    "wrote_max_energy": True,
                },
            }
        )

        self.assertEqual(result.previous_energy, 1)
        self.assertEqual(result.requested_energy, 100)
        self.assertEqual(result.previous_max_energy, 3)
        self.assertEqual(result.requested_max_energy, 100)
        self.assertTrue(result.wrote_max_energy)

    def test_gold_write_result_parses_write_metadata(self) -> None:
        result = ManagedGoldWriteResult.from_payload(
            {
                "pid": 2348,
                "runtime_version": "9.0.0",
                "floor": 11,
                "ascension": 4,
                "gold": 999,
                "hp": 23,
                "max_hp": 80,
                "block": 0,
                "energy": 3,
                "max_energy": 3,
                "write": {
                    "field": "player_gold",
                    "gold_address": "0x857bbb38",
                    "previous_gold": 538,
                    "requested_gold": 999,
                    "ui_address": "0x84c2c44c",
                    "previous_ui_gold": 538,
                    "wrote_ui_gold": True,
                },
            }
        )

        self.assertEqual(result.previous_gold, 538)
        self.assertEqual(result.requested_gold, 999)
        self.assertEqual(result.ui_address, "0x84c2c44c")
        self.assertTrue(result.wrote_ui_gold)


if __name__ == "__main__":
    unittest.main()
