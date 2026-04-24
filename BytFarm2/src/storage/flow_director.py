"""
storage/flow_director.py — FlowDirector (Phase 1)
===================================================
Write batching, staging zones, and VRAM runway allocation.
Single pre-allocated contiguous file per intent type.
Flush failures surface to StorageHealth for the UI.
"""

from __future__ import annotations
import logging
import pathlib
import time
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


class StorageHealth:
    """Simple flag store surfaced to the UI STORAGE tab."""
    _flags: Dict[str, bool] = {}

    @classmethod
    def set_flag(cls, key: str, value: bool) -> None:
        if cls._flags.get(key) != value:
            log.warning(f'[StorageHealth] {key}={value}')
        cls._flags[key] = value

    @classmethod
    def get_flags(cls) -> Dict[str, bool]:
        return dict(cls._flags)

    @classmethod
    def healthy(cls) -> bool:
        return not any(cls._flags.values())


class FlowDirector:
    """
    Phase 1 StorageController component.
    Batches writes into pre-allocated contiguous files per intent type.
    """

    PREALLOC_SIZES: Dict[str, int] = {
        'ghost_spill':  256 * 1024 * 1024,   # 256 MB
        'vram_runway':  512 * 1024 * 1024,   # 512 MB (mode-overridable)
        'general':       64 * 1024 * 1024,   #  64 MB
    }

    def __init__(self, staging_dir: pathlib.Path,
                 batch_flush_mb: float = 4.0) -> None:
        self._dir              = staging_dir
        self._batch_flush_bytes = int(batch_flush_mb * 1024 * 1024)
        self._batch_flush_s    = 2.0
        self._queue: List[dict] = []
        self._queued_bytes      = 0
        self._last_flush        = time.monotonic()
        self._file_handles: Dict[str, object] = {}
        self._file_offsets: Dict[str, int]    = {}
        self._file_sizes:   Dict[str, int]    = {}

    def stage_write(self, intent: str,
                    data: Optional[bytes] = None,
                    size_bytes: int = 0) -> bool:
        """
        Queue a write for batching.
        intent: 'ghost_spill' | 'vram_runway' | 'general'
        Returns True if accepted.
        """
        size = size_bytes or (len(data) if data else 0)
        self._queue.append({'intent': intent, 'data': data, 'size': size})
        self._queued_bytes += size
        return True

    def flush(self, force: bool = False) -> int:
        """
        Flush staged writes to disk if batch threshold reached.
        force=True: flush regardless (e.g. on shutdown).
        Returns bytes written.
        """
        age = time.monotonic() - self._last_flush
        if (not force
                and self._queued_bytes < self._batch_flush_bytes
                and age < self._batch_flush_s):
            return 0
        written = self._do_flush()
        self._last_flush = time.monotonic()
        return written

    def allocate_vram_runway(self, size_bytes: int, mode: str) -> Optional[str]:
        """
        Reserve a contiguous region for pseudo-VRAM.
        Returns region_id (file path string) or None on failure.
        """
        fname = f'vram_runway_{mode}_{int(time.time())}.bin'
        path  = self._dir / fname
        try:
            with open(path, 'wb') as f:
                f.seek(size_bytes - 1)
                f.write(b'\x00')   # pre-allocate contiguous space
            StorageHealth.set_flag('vram_runway_ok', True)
            return str(path)
        except OSError as e:
            log.error(f'[FlowDirector] VRAM runway allocation failed: {e}')
            StorageHealth.set_flag('vram_runway_ok', False)
            return None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _ensure_file(self, intent: str) -> bool:
        if intent in self._file_handles:
            return True
        path = self._dir / f'{intent}.bin'
        try:
            size = self.PREALLOC_SIZES.get(intent, 64 * 1024 * 1024)
            if path.exists():
                fh = open(path, 'r+b')
            else:
                fh = open(path, 'w+b')
            if path.stat().st_size < size:
                fh.seek(size - 1)
                fh.write(b'\x00')  # pre-allocate
            fh.seek(0)
            self._file_handles[intent] = fh
            self._file_offsets[intent] = 0
            self._file_sizes[intent]   = size
            return True
        except OSError as e:
            log.error(f'[FlowDirector] Cannot open staging file {intent}: {e}')
            return False

    def _do_flush(self) -> int:
        written = 0
        failed  = False
        for item in self._queue:
            intent = item['intent']
            data   = item.get('data') or b'\x00' * max(item['size'], 0)
            if not data:
                continue
            if not self._ensure_file(intent):
                failed = True
                continue
            fh     = self._file_handles[intent]
            offset = self._file_offsets[intent]
            size   = self._file_sizes[intent]
            # Wrap around at end of pre-allocated region
            if offset + len(data) > size:
                offset = 0
            fh.seek(offset)
            fh.write(data)
            self._file_offsets[intent] = offset + len(data)
            written += len(data)
        self._queue.clear()
        self._queued_bytes = 0
        StorageHealth.set_flag('flush_error', failed)
        return written

    def close(self) -> None:
        """Flush remaining data and close all file handles."""
        self.flush(force=True)
        for fh in self._file_handles.values():
            try:
                fh.close()
            except Exception:
                pass
        self._file_handles.clear()


def get_staging_dir(config=None) -> pathlib.Path:
    """
    Returns the staging directory path.
    Default: C:\\BytFarm\\staging (OS drive, lowest cross-drive overhead).
    Configurable via storage.staging_path in config.toml.
    """
    import os
    configured = config.get('storage.staging_path', '') if config else ''
    if configured:
        path = pathlib.Path(configured)
    else:
        os_drive = pathlib.Path(os.environ.get('SystemDrive', 'C:') + '\\')
        path = os_drive / 'BytFarm' / 'staging'

    try:
        path.mkdir(parents=True, exist_ok=True)
        # Write test to confirm access
        test = path / '.write_test'
        test.write_bytes(b'ok')
        test.unlink()
    except OSError as e:
        raise RuntimeError(
            f'BytFarm cannot write to staging directory {path}: {e}\n'
            f'Check permissions or set storage.staging_path in config.toml'
        )

    return path
