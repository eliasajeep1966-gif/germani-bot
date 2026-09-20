import asyncio
import hashlib
import json
import os
from pathlib import Path
from config import BASE_DIR

# F-03: server-side map for short file callbacks (keeps callback_data <= 64 bytes).
FILENAME_MAP = {}


def get_short_hash(text: str) -> str:
    """Return a 10-char hex digest for callback_data-safe file references."""
    return hashlib.md5(text.encode()).hexdigest()[:10]


def map_filename(filename: str) -> str:
    """Store filename under its short hash and return the hash for callbacks."""
    h = get_short_hash(filename)
    FILENAME_MAP[h] = filename
    return h


def resolve_filename(hash_val: str) -> str:
    """Resolve a callback hash back to the stored filename (None if expired/unknown)."""
    return FILENAME_MAP.get(hash_val)

# MED-002, MED-003: Auto-invalidating catalog cache
_CATALOG_CACHE = None
_LAST_MTIME = 0

# State Pointers refactor: auto-invalidating parsed-content cache
_CONTENT_CACHE = {}
_CONTENT_LAST_MTIME = 0


def _data_max_mtime() -> float:
    """Max mtime of data/b1/ and its skill/teil subdirectories (cheap change probe)."""
    data_dir = Path(BASE_DIR, "data", "b1").resolve()
    max_mtime = 0.0
    try:
        if data_dir.exists():
            max_mtime = os.path.getmtime(data_dir)
            for skill_dir in data_dir.iterdir():
                if skill_dir.is_dir():
                    try:
                        mtime = os.path.getmtime(skill_dir)
                        if mtime > max_mtime:
                            max_mtime = mtime
                        for teil_dir in skill_dir.iterdir():
                            if teil_dir.is_dir():
                                mtime = os.path.getmtime(teil_dir)
                                if mtime > max_mtime:
                                    max_mtime = mtime
                    except OSError:
                        pass
    except OSError:
        pass
    return max_mtime


def _build_catalog_sync():
    """Synchronous (blocking) catalog builder — MUST run via asyncio.to_thread.

    Contains all os.listdir / os.path.getmtime blocking I/O for get_catalog
    so the event loop is never blocked. Returns (catalog, max_mtime).
    """
    data_dir = Path(BASE_DIR, "data", "b1").resolve()
    max_mtime = 0.0
    catalog = {}
    try:
        if os.path.exists(data_dir):
            try:
                max_mtime = max(max_mtime, os.path.getmtime(data_dir))
            except OSError:
                pass
            try:
                skill_names = os.listdir(data_dir)
            except OSError:
                return catalog, max_mtime
            for skill_name in skill_names:
                skill_path = os.path.join(data_dir, skill_name)
                try:
                    if not os.path.isdir(skill_path):
                        continue
                    max_mtime = max(max_mtime, os.path.getmtime(skill_path))
                except OSError:
                    continue
                skill = skill_name.lower()
                catalog[skill] = {}
                try:
                    teil_names = os.listdir(skill_path)
                except OSError:
                    continue
                for teil_name in teil_names:
                    teil_path = os.path.join(skill_path, teil_name)
                    try:
                        if not os.path.isdir(teil_path):
                            continue
                        max_mtime = max(max_mtime, os.path.getmtime(teil_path))
                    except OSError:
                        continue
                    teil = teil_name.lower()
                    try:
                        files = sorted([f for f in os.listdir(teil_path) if f.endswith('.json')])
                    except OSError:
                        continue
                    if files:
                        catalog[skill][teil] = files
    except OSError:
        pass
    return catalog, max_mtime


async def get_catalog() -> dict:
    """
    Build or return cached catalog of data/b1 structure.
    Cache invalidates when any file/directory mtime changes.
    Returns: {skill: {teil: [sorted_file_list], ...}, ...}
    F5/F6/F7: all blocking os.listdir / os.path.getmtime I/O lives in
    _build_catalog_sync() and runs via asyncio.to_thread — never on the loop.
    """
    global _CATALOG_CACHE, _LAST_MTIME

    max_mtime = await asyncio.to_thread(_data_max_mtime)

    if _CATALOG_CACHE is not None and max_mtime <= _LAST_MTIME:
        return _CATALOG_CACHE

    catalog, fresh_mtime = await asyncio.to_thread(_build_catalog_sync)

    _CATALOG_CACHE = catalog
    _LAST_MTIME = max(max_mtime, fresh_mtime)
    return catalog


async def get_content(skill: str, teil: str, file_name: str) -> dict:
    """
    Fetch parsed JSON content for one quiz file via an auto-invalidating cache.
    If the data tree changed since the last build, the whole cache is dropped
    and rebuilt on demand. File reads run in a worker thread (stdlib only, no new deps).
    Raises FileNotFoundError / ValueError on missing/corrupt content.
    """
    global _CONTENT_CACHE, _CONTENT_LAST_MTIME

    # F-01: never run blocking FS probes on the event loop.
    max_mtime = await asyncio.to_thread(_data_max_mtime)
    if max_mtime > _CONTENT_LAST_MTIME:
        _CONTENT_CACHE = {}
        _CONTENT_LAST_MTIME = max_mtime

    safe_name = os.path.basename(file_name)
    # F-12: composite key prevents cross-skill/teil collisions on identical basenames.
    cache_key = f"{skill.lower().strip()}_{teil.lower().strip()}_{safe_name}"
    if cache_key not in _CONTENT_CACHE:
        # F-01: find_directory_case_insensitive does os.listdir (blocking) — off-loop.
        target_dir = await asyncio.to_thread(
            find_directory_case_insensitive,
            f"data/b1/{skill.lower().strip()}", teil.lower().strip()
        )
        if not target_dir:
            raise FileNotFoundError(f"Unknown content dir: {skill}/{teil}")
        allowed_data_dir = Path(BASE_DIR, "data").resolve()
        file_path = (Path(target_dir) / safe_name).resolve()
        try:
            if not file_path.is_relative_to(allowed_data_dir):
                raise FileNotFoundError(f"Blocked path escape: {safe_name}")
        except (ValueError, AttributeError):
            raise FileNotFoundError(f"Blocked path escape: {safe_name}")
        # F-01: is_file() is a stat syscall — off-loop.
        if not await asyncio.to_thread(file_path.is_file):
            raise FileNotFoundError(f"Missing content file: {safe_name}")

        def _load():
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)

        parsed = await asyncio.to_thread(_load)
        if not isinstance(parsed, (dict, list)):
            raise ValueError(f"Unsupported content shape: {safe_name}")
        _CONTENT_CACHE[cache_key] = parsed

    return _CONTENT_CACHE[cache_key]


def render_progress_bar(percentage: float, total_blocks: int = 10, filled: str = "🟩", empty: str = "⬜") -> str:
    """LOW-013: single shared progress-bar renderer (percentage 0-100 -> block string)."""
    try:
        pct = max(0.0, min(100.0, float(percentage)))
    except (TypeError, ValueError):
        pct = 0.0
    filled_blocks = int(round((pct / 100) * total_blocks))
    filled_blocks = max(0, min(total_blocks, filled_blocks))
    return filled * filled_blocks + empty * (total_blocks - filled_blocks)


def find_directory_case_insensitive(base_path, target_dir):
    """
    دالة مساعدة للبحث عن المجلدات دون الحساسية لحالة الأحرف (Case-Insensitive)
    مع تأمين المسار ضد ثغرات Path Traversal.
    """
    safe_target = os.path.basename(target_dir)
    
    allowed_data_dir = Path(BASE_DIR, "data").resolve()
    full_base_path = Path(BASE_DIR, base_path).resolve()
    
    try:
        if not full_base_path.is_relative_to(allowed_data_dir):
            return None
    except (ValueError, AttributeError):
        return None

    if not full_base_path.exists() or not full_base_path.is_dir():
        return None

    for item in os.listdir(full_base_path):
        if item.lower() == safe_target.lower():
            resolved_path = (full_base_path / item).resolve()
            try:
                if resolved_path.is_relative_to(allowed_data_dir) and resolved_path.is_dir():
                    return str(resolved_path)
            except (ValueError, AttributeError):
                return None

    return None