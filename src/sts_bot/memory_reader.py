from __future__ import annotations

import ctypes
import struct
import time
from dataclasses import dataclass, field
from ctypes import wintypes

from sts_bot.config import MemoryFieldDefinition
from sts_bot.models import StateSource

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_MODULE_NAME32 = 255
MAX_PATH = 260
LIST_MODULES_ALL = 0x03


OpenProcess = kernel32.OpenProcess
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenProcess.restype = wintypes.HANDLE

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.LPVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
ReadProcessMemory.restype = wintypes.BOOL

CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
CreateToolhelp32Snapshot.restype = wintypes.HANDLE

Module32FirstW = kernel32.Module32FirstW
Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
Module32FirstW.restype = wintypes.BOOL

Module32NextW = kernel32.Module32NextW
Module32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
Module32NextW.restype = wintypes.BOOL

EnumProcessModulesEx = psapi.EnumProcessModulesEx
EnumProcessModulesEx.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.HMODULE),
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.DWORD,
]
EnumProcessModulesEx.restype = wintypes.BOOL

GetModuleBaseNameW = psapi.GetModuleBaseNameW
GetModuleBaseNameW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
GetModuleBaseNameW.restype = wintypes.DWORD

GetModuleFileNameExW = psapi.GetModuleFileNameExW
GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
GetModuleFileNameExW.restype = wintypes.DWORD


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * (MAX_MODULE_NAME32 + 1)),
        ("szExePath", wintypes.WCHAR * MAX_PATH),
    ]


class MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", wintypes.LPVOID),
        ("SizeOfImage", wintypes.DWORD),
        ("EntryPoint", wintypes.LPVOID),
    ]


GetModuleInformation = psapi.GetModuleInformation
GetModuleInformation.argtypes = [
    wintypes.HANDLE,
    wintypes.HMODULE,
    ctypes.POINTER(MODULEINFO),
    wintypes.DWORD,
]
GetModuleInformation.restype = wintypes.BOOL


@dataclass(slots=True)
class ProcessModule:
    name: str
    base_address: int
    size: int
    path: str = ""


@dataclass(slots=True)
class MemoryFieldResult:
    name: str
    value: int | None = None
    source: str | None = None
    error: str | None = None
    locator_address: int | None = None
    resolved_address: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "source": self.source,
            "error": self.error,
            "locator_address": self.locator_address,
            "resolved_address": self.resolved_address,
        }


@dataclass(slots=True)
class MemoryReadSnapshot:
    pid: int
    module: str
    values: dict[str, int | None] = field(default_factory=dict)
    fields: dict[str, MemoryFieldResult] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    captured_at: float = 0.0
    cached: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "module": self.module,
            "values": dict(self.values),
            "fields": {name: result.to_dict() for name, result in self.fields.items()},
            "errors": self.errors[:],
            "captured_at": self.captured_at,
            "cached": self.cached,
        }

    def as_cached(self) -> "MemoryReadSnapshot":
        return MemoryReadSnapshot(
            pid=self.pid,
            module=self.module,
            values=dict(self.values),
            fields={name: MemoryFieldResult(**result.to_dict()) for name, result in self.fields.items()},
            errors=self.errors[:],
            captured_at=self.captured_at,
            cached=True,
        )


class ProcessMemoryReader:
    def __init__(self, *, pid: int, module: str, refresh_ms: int = 250) -> None:
        self.pid = pid
        self.module = module
        self.refresh_ms = max(0, refresh_ms)
        self._handle: int | None = None
        self._modules: dict[str, ProcessModule] = {}
        self._signature_cache: dict[tuple[str, str], int | None] = {}
        self._last_snapshot: MemoryReadSnapshot | None = None
        self._last_field_names: tuple[str, ...] = ()
        self._last_read_at = 0.0

    def close(self) -> None:
        if self._handle:
            CloseHandle(self._handle)
            self._handle = None

    def read_fields(self, fields: list[MemoryFieldDefinition]) -> MemoryReadSnapshot:
        field_names = tuple(field.name for field in fields)
        if (
            self._last_snapshot is not None
            and field_names == self._last_field_names
            and self.refresh_ms > 0
            and (time.time() - self._last_read_at) * 1000 < self.refresh_ms
        ):
            return self._last_snapshot.as_cached()

        snapshot = MemoryReadSnapshot(pid=self.pid, module=self.module, captured_at=time.time())
        try:
            self._ensure_process()
            self._modules = self._module_table()
        except Exception as exc:
            snapshot.errors.append(str(exc))
            for field in fields:
                snapshot.fields[field.name] = MemoryFieldResult(name=field.name, error=str(exc))
                snapshot.values[field.name] = None
            self._remember_snapshot(field_names, snapshot)
            return snapshot

        for field in fields:
            result = self._read_field_result(field)
            snapshot.fields[field.name] = result
            snapshot.values[field.name] = result.value
        self._remember_snapshot(field_names, snapshot)
        return snapshot

    def _remember_snapshot(self, field_names: tuple[str, ...], snapshot: MemoryReadSnapshot) -> None:
        self._last_snapshot = snapshot
        self._last_field_names = field_names
        self._last_read_at = time.time()

    def _ensure_process(self) -> None:
        if self._handle is not None:
            return
        handle = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, self.pid)
        if not handle:
            raise RuntimeError(f"OpenProcess failed for pid={self.pid}")
        self._handle = handle

    def _module_table(self) -> dict[str, ProcessModule]:
        psapi_error: Exception | None = None
        try:
            modules = self._module_table_from_psapi()
        except Exception as exc:
            psapi_error = exc
        else:
            if modules:
                return modules
        try:
            return self._module_table_from_toolhelp()
        except Exception:
            if psapi_error is not None:
                raise psapi_error
            raise

    def _module_table_from_psapi(self) -> dict[str, ProcessModule]:
        self._ensure_process()
        assert self._handle is not None
        needed = wintypes.DWORD()
        handles = (wintypes.HMODULE * 1024)()
        success = EnumProcessModulesEx(self._handle, handles, ctypes.sizeof(handles), ctypes.byref(needed), LIST_MODULES_ALL)
        if not success:
            raise RuntimeError(f"EnumProcessModulesEx failed for pid={self.pid}")
        required_count = needed.value // ctypes.sizeof(wintypes.HMODULE)
        if required_count > len(handles):
            handles = (wintypes.HMODULE * required_count)()
            success = EnumProcessModulesEx(
                self._handle,
                handles,
                ctypes.sizeof(handles),
                ctypes.byref(needed),
                LIST_MODULES_ALL,
            )
            if not success:
                raise RuntimeError(f"EnumProcessModulesEx failed for pid={self.pid}")
        modules: dict[str, ProcessModule] = {}
        handle_count = needed.value // ctypes.sizeof(wintypes.HMODULE)
        for module_handle in handles[:handle_count]:
            name_buffer = ctypes.create_unicode_buffer(MAX_MODULE_NAME32 + 1)
            path_buffer = ctypes.create_unicode_buffer(MAX_PATH * 4)
            info = MODULEINFO()
            if not GetModuleBaseNameW(self._handle, module_handle, name_buffer, len(name_buffer)):
                continue
            path = ""
            if GetModuleFileNameExW(self._handle, module_handle, path_buffer, len(path_buffer)):
                path = path_buffer.value
            if not GetModuleInformation(self._handle, module_handle, ctypes.byref(info), ctypes.sizeof(info)):
                continue
            base_address = ctypes.cast(info.lpBaseOfDll, ctypes.c_void_p).value
            if base_address is None:
                continue
            module = ProcessModule(
                name=name_buffer.value,
                base_address=int(base_address),
                size=int(info.SizeOfImage),
                path=path,
            )
            modules[module.name.lower()] = module
        return modules

    def _module_table_from_toolhelp(self) -> dict[str, ProcessModule]:
        snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if snapshot == INVALID_HANDLE_VALUE:
            raise RuntimeError(f"CreateToolhelp32Snapshot failed for pid={self.pid}")
        modules: dict[str, ProcessModule] = {}
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
            if not Module32FirstW(snapshot, ctypes.byref(entry)):
                raise RuntimeError(f"Module32FirstW failed for pid={self.pid}")
            while True:
                base_pointer = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value
                if base_pointer is not None:
                    module = ProcessModule(
                        name=str(entry.szModule),
                        base_address=int(base_pointer),
                        size=int(entry.modBaseSize),
                        path=str(entry.szExePath),
                    )
                    modules[module.name.lower()] = module
                if not Module32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            CloseHandle(snapshot)
        return modules

    def _read_field_result(self, field: MemoryFieldDefinition) -> MemoryFieldResult:
        try:
            module = self._resolve_module(self.module)
            locator_address = self._resolve_locator(field, module)
            resolved_address = self._resolve_pointer_chain(locator_address, field.pointer_offsets)
            value = self._read_typed_value(resolved_address, field.value_type)
            return MemoryFieldResult(
                name=field.name,
                value=value,
                source=StateSource.MEMORY.value,
                locator_address=locator_address,
                resolved_address=resolved_address,
            )
        except Exception as exc:
            return MemoryFieldResult(name=field.name, error=str(exc))

    def _resolve_module(self, name: str) -> ProcessModule:
        module = self._modules.get(name.lower())
        if module is None:
            raise RuntimeError(f"Module not found: {name}")
        return module

    def _resolve_locator(self, field: MemoryFieldDefinition, module: ProcessModule) -> int:
        if field.locator_kind == "module_offset":
            if field.offset is None:
                raise RuntimeError(f"Field {field.name} is missing offset")
            return module.base_address + field.offset
        if field.locator_kind == "signature":
            if not field.pattern:
                raise RuntimeError(f"Field {field.name} is missing pattern")
            match_address = self._signature_match_address(module, field.pattern)
            if match_address is None:
                raise RuntimeError(f"Signature not found for field {field.name}")
            return match_address + field.pattern_offset
        raise RuntimeError(f"Unsupported locator kind: {field.locator_kind}")

    def _signature_match_address(self, module: ProcessModule, pattern: str) -> int | None:
        cache_key = (module.name.lower(), pattern)
        if cache_key in self._signature_cache:
            return self._signature_cache[cache_key]
        raw = self._read_raw_bytes(module.base_address, module.size)
        tokens = self._parse_signature(pattern)
        token_count = len(tokens)
        match_address: int | None = None
        for index in range(max(0, len(raw) - token_count + 1)):
            if all(token is None or raw[index + offset] == token for offset, token in enumerate(tokens)):
                match_address = module.base_address + index
                break
        self._signature_cache[cache_key] = match_address
        return match_address

    @staticmethod
    def _parse_signature(pattern: str) -> list[int | None]:
        tokens: list[int | None] = []
        for token in pattern.split():
            if token in {"??", "?"}:
                tokens.append(None)
                continue
            tokens.append(int(token, 16))
        if not tokens:
            raise RuntimeError("Signature pattern is empty")
        return tokens

    def _resolve_pointer_chain(self, address: int, offsets: list[int]) -> int:
        current = address
        for offset in offsets:
            current = self._read_pointer(current) + offset
        return current

    def _read_pointer(self, address: int) -> int:
        data = self._read_raw_bytes(address, 8)
        return struct.unpack("<Q", data)[0]

    def _read_typed_value(self, address: int, value_type: str) -> int:
        format_map = {
            "int8": ("<b", 1),
            "uint8": ("<B", 1),
            "int16": ("<h", 2),
            "uint16": ("<H", 2),
            "int32": ("<i", 4),
            "uint32": ("<I", 4),
            "int64": ("<q", 8),
            "uint64": ("<Q", 8),
        }
        format_info = format_map.get(value_type.lower())
        if format_info is None:
            raise RuntimeError(f"Unsupported value type: {value_type}")
        struct_format, size = format_info
        data = self._read_raw_bytes(address, size)
        return int(struct.unpack(struct_format, data)[0])

    def _read_raw_bytes(self, address: int, size: int) -> bytes:
        self._ensure_process()
        assert self._handle is not None
        buffer = (ctypes.c_ubyte * size)()
        read = ctypes.c_size_t()
        success = ReadProcessMemory(
            self._handle,
            ctypes.c_void_p(address),
            ctypes.byref(buffer),
            size,
            ctypes.byref(read),
        )
        if not success or read.value != size:
            raise RuntimeError(f"ReadProcessMemory failed at 0x{address:x} size={size}")
        return bytes(buffer[: read.value])
