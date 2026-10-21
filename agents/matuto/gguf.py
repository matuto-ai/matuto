"""Read the architecture's trained context length from a GGUF header."""

from functools import lru_cache
from pathlib import Path
import struct


_SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_INTEGER_TYPES = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 10: "<Q", 11: "<q"}
_MAX_METADATA_ENTRIES = 100_000
_MAX_ARRAY_ITEMS = 4_000_000
_MAX_KEY_BYTES = 1024 * 1024


def trained_context(path):
    """Return GGUF's architecture-specific context_length, or None if unreadable."""
    try:
        path = Path(path).resolve()
        stat = path.stat()
        return _trained_context(str(path), stat.st_ino, stat.st_size,
                                stat.st_mtime_ns, stat.st_ctime_ns)
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=128)
def _trained_context(path, inode, size, mtime_ns, ctime_ns):
    del inode, mtime_ns, ctime_ns
    try:
        with open(path, "rb") as model:
            if _read(model, 4) != b"GGUF":
                return None
            version, _tensor_count, metadata_count = struct.unpack("<IQQ", _read(model, 20))
            if version not in (1, 2, 3) or metadata_count > _MAX_METADATA_ENTRIES:
                return None
            architecture = None
            contexts = {}
            for _ in range(metadata_count):
                key = _string(model)
                kind = _u32(model)
                if key == "general.architecture" and kind == 8:
                    architecture = _string(model, _MAX_KEY_BYTES)
                elif key.endswith(".context_length") and kind in _INTEGER_TYPES:
                    value = struct.unpack(_INTEGER_TYPES[kind], _read(model, _SCALAR_SIZES[kind]))[0]
                    contexts[key] = value
                else:
                    _skip(model, kind)
                if model.tell() > size:
                    return None
            value = contexts.get(f"{architecture}.context_length")
            return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
    except (OSError, OverflowError, struct.error, ValueError):
        return None


def _read(stream, count):
    if count < 0:
        raise ValueError("negative GGUF field size")
    result = stream.read(count)
    if len(result) != count:
        raise ValueError("truncated GGUF metadata")
    return result


def _u32(stream):
    return struct.unpack("<I", _read(stream, 4))[0]


def _u64(stream):
    return struct.unpack("<Q", _read(stream, 8))[0]


def _string(stream, maximum=None):
    length = _u64(stream)
    if length > (_MAX_KEY_BYTES if maximum is None else maximum):
        raise ValueError("oversized GGUF string")
    try:
        return _read(stream, length).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("invalid GGUF string") from None


def _skip_string(stream):
    length = _u64(stream)
    stream.seek(length, 1)


def _skip(stream, kind):
    size = _SCALAR_SIZES.get(kind)
    if size is not None:
        stream.seek(size, 1)
    elif kind == 8:
        _skip_string(stream)
    elif kind == 9:
        element_type = _u32(stream)
        count = _u64(stream)
        if count > _MAX_ARRAY_ITEMS:
            raise ValueError("oversized GGUF array")
        element_size = _SCALAR_SIZES.get(element_type)
        if element_size is not None:
            stream.seek(element_size * count, 1)
        elif element_type == 8:
            for _ in range(count):
                _skip_string(stream)
        else:
            raise ValueError("unsupported GGUF array element type")
    else:
        raise ValueError("unsupported GGUF metadata type")
