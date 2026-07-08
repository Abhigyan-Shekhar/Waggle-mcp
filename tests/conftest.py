from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Auto-load sqlite-vec and register vec_decode_embedding on all test sqlite3 connections

_original_connect = sqlite3.connect


def _patched_connect(*args, **kwargs):
    conn = _original_connect(*args, **kwargs)
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except Exception:
        pass

    # Register custom vec_decode_embedding for triggers on direct connections
    try:
        # Resolve dimension from vec_nodes schema if it exists
        dim = 8
        try:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE name='vec_nodes'").fetchone()
            if row:
                m = re.search(r"float\[(\d+)\]", row[0])
                if m:
                    dim = int(m.group(1))
        except Exception:
            pass

        # Strip header b"WEB1" (4 bytes) and trailer CRC (4 bytes)
        def _decode_trigger_blob(blob: bytes | None) -> bytes | None:
            if not blob:
                return None
            if len(blob) == (dim * 4) + 8 and blob.startswith(b"WEB1"):
                return blob[4:-4]
            if len(blob) == dim * 4:
                return blob
            # Return zero vector for corrupt/mismatched sizes
            return b"\x00" * (dim * 4)

        conn.create_function("vec_decode_embedding", 1, _decode_trigger_blob)
    except Exception:
        pass
    return conn


sqlite3.connect = _patched_connect
