from __future__ import annotations

import struct
import unittest

from sts_bot.config import MemoryFieldDefinition
from sts_bot.memory_reader import ProcessMemoryReader, ProcessModule


class FakeMemoryReader(ProcessMemoryReader):
    def __init__(self, *, modules: dict[str, ProcessModule], memory: dict[int, int]) -> None:
        super().__init__(pid=1234, module="sts2.dll", refresh_ms=0)
        self._fake_modules = {name.lower(): module for name, module in modules.items()}
        self._fake_memory = memory

    def _ensure_process(self) -> None:
        self._handle = 1

    def _module_table(self) -> dict[str, ProcessModule]:
        return dict(self._fake_modules)

    def _read_raw_bytes(self, address: int, size: int) -> bytes:
        try:
            return bytes(self._fake_memory[address + index] for index in range(size))
        except KeyError as exc:
            raise RuntimeError(f"missing fake memory at 0x{address:x}") from exc


class ModuleDiscoveryReader(ProcessMemoryReader):
    def __init__(self, *, psapi_modules: dict[str, ProcessModule] | None, toolhelp_modules: dict[str, ProcessModule] | None) -> None:
        super().__init__(pid=1234, module="sts2.dll", refresh_ms=0)
        self._psapi_modules = psapi_modules
        self._toolhelp_modules = toolhelp_modules

    def _ensure_process(self) -> None:
        self._handle = 1

    def _module_table_from_psapi(self) -> dict[str, ProcessModule]:
        if self._psapi_modules is None:
            raise RuntimeError("psapi failed")
        return {name.lower(): module for name, module in self._psapi_modules.items()}

    def _module_table_from_toolhelp(self) -> dict[str, ProcessModule]:
        if self._toolhelp_modules is None:
            raise RuntimeError("toolhelp failed")
        return {name.lower(): module for name, module in self._toolhelp_modules.items()}


class MemoryReaderTest(unittest.TestCase):
    def test_module_offset_reads_scalar_value(self) -> None:
        module = ProcessModule(name="sts2.dll", base_address=0x1000, size=0x200)
        memory = {}
        memory.update(_pack_bytes(0x1010, struct.pack("<i", 77)))
        reader = FakeMemoryReader(modules={"sts2.dll": module}, memory=memory)

        snapshot = reader.read_fields(
            [
                MemoryFieldDefinition(
                    name="gold",
                    locator_kind="module_offset",
                    offset=0x10,
                    value_type="int32",
                )
            ]
        )

        self.assertEqual(snapshot.values["gold"], 77)
        self.assertEqual(snapshot.fields["gold"].resolved_address, 0x1010)
        self.assertIsNone(snapshot.fields["gold"].error)

    def test_signature_locator_reads_scalar_value(self) -> None:
        module = ProcessModule(name="sts2.dll", base_address=0x2000, size=0x0C)
        blob = b"\x90\x90\x48\x8B\x11\x22\x33\x90\x90\x90\x90\x90"
        memory = _pack_bytes(0x2000, blob)
        memory.update(_pack_bytes(0x2008, struct.pack("<i", 99)))
        reader = FakeMemoryReader(modules={"sts2.dll": module}, memory=memory)

        snapshot = reader.read_fields(
            [
                MemoryFieldDefinition(
                    name="floor",
                    locator_kind="signature",
                    pattern="48 8B 11 22",
                    pattern_offset=6,
                    value_type="int32",
                )
            ]
        )

        self.assertEqual(snapshot.values["floor"], 99)
        self.assertEqual(snapshot.fields["floor"].locator_address, 0x2008)

    def test_pointer_chain_resolves_nested_addresses(self) -> None:
        module = ProcessModule(name="sts2.dll", base_address=0x3000, size=0x100)
        memory = {}
        memory.update(_pack_bytes(0x3010, struct.pack("<Q", 0x4000)))
        memory.update(_pack_bytes(0x4020, struct.pack("<Q", 0x5000)))
        memory.update(_pack_bytes(0x5030, struct.pack("<i", 61)))
        reader = FakeMemoryReader(modules={"sts2.dll": module}, memory=memory)

        snapshot = reader.read_fields(
            [
                MemoryFieldDefinition(
                    name="hp",
                    locator_kind="module_offset",
                    offset=0x10,
                    pointer_offsets=[0x20, 0x30],
                    value_type="int32",
                )
            ]
        )

        self.assertEqual(snapshot.values["hp"], 61)
        self.assertEqual(snapshot.fields["hp"].resolved_address, 0x5030)

    def test_structured_failures_are_reported_per_field(self) -> None:
        module = ProcessModule(name="sts2.dll", base_address=0x6000, size=0x100)
        reader = FakeMemoryReader(modules={"sts2.dll": module}, memory={})

        snapshot = reader.read_fields(
            [
                MemoryFieldDefinition(
                    name="energy",
                    locator_kind="module_offset",
                    offset=0x10,
                    value_type="int32",
                )
            ]
        )

        self.assertIsNone(snapshot.values["energy"])
        self.assertIn("missing fake memory", snapshot.fields["energy"].error or "")

    def test_module_table_prefers_psapi_results(self) -> None:
        psapi_module = ProcessModule(name="sts2.dll", base_address=0x7000, size=0x200)
        toolhelp_module = ProcessModule(name="sts2.dll", base_address=0x8000, size=0x300)
        reader = ModuleDiscoveryReader(
            psapi_modules={"sts2.dll": psapi_module},
            toolhelp_modules={"sts2.dll": toolhelp_module},
        )

        modules = reader._module_table()

        self.assertEqual(modules["sts2.dll"].base_address, 0x7000)
        self.assertEqual(modules["sts2.dll"].size, 0x200)

    def test_module_table_falls_back_to_toolhelp(self) -> None:
        toolhelp_module = ProcessModule(name="sts2.dll", base_address=0x8000, size=0x300)
        reader = ModuleDiscoveryReader(
            psapi_modules=None,
            toolhelp_modules={"sts2.dll": toolhelp_module},
        )

        modules = reader._module_table()

        self.assertEqual(modules["sts2.dll"].base_address, 0x8000)
        self.assertEqual(modules["sts2.dll"].size, 0x300)


def _pack_bytes(address: int, payload: bytes) -> dict[int, int]:
    return {address + index: value for index, value in enumerate(payload)}


if __name__ == "__main__":
    unittest.main()
