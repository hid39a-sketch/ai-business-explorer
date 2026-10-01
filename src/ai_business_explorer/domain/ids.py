"""UUIDv7 の生成（Python 3.12 標準ライブラリには uuid7 がないため自前で実装）。

ビット配置（RFC 9562）: unix_ts_ms(48) | ver(4) | rand_a(12) | var(2) | rand_b(62)
"""

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    unix_ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand & 0xFFF
    rand_b = (rand >> 12) & ((1 << 62) - 1)
    value = (unix_ts_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)
