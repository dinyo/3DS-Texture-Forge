"""Small pure-Python CityHash64 implementation.

Azahar/Citra use Common::ComputeHash64 for custom texture hashes, which is
CityHash64. This module implements the 64-bit path used for byte strings.
"""

from __future__ import annotations


MASK64 = 0xFFFFFFFFFFFFFFFF
K0 = 0xC3A5C85C97CB3127
K1 = 0xB492B66FBE98F273
K2 = 0x9AE16A3B2F90404F


def _u64(value: int) -> int:
    return value & MASK64


def _fetch64(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos:pos + 8], "little")


def _fetch32(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos:pos + 4], "little")


def _rotate(value: int, shift: int) -> int:
    value &= MASK64
    if shift == 0:
        return value
    return ((value >> shift) | (value << (64 - shift))) & MASK64


def _shift_mix(value: int) -> int:
    return value ^ (value >> 47)


def _bswap64(value: int) -> int:
    return int.from_bytes((value & MASK64).to_bytes(8, "little"), "big")


def _hash_len16_mul(u: int, v: int, mul: int) -> int:
    a = _u64((u ^ v) * mul)
    a ^= a >> 47
    b = _u64((v ^ a) * mul)
    b ^= b >> 47
    return _u64(b * mul)


def _hash_len16(u: int, v: int) -> int:
    return _hash_len16_mul(u, v, 0x9DDFEA08EB382D69)


def _hash_len0to16(data: bytes, pos: int, length: int) -> int:
    if length >= 8:
        mul = K2 + length * 2
        a = _fetch64(data, pos) + K2
        b = _fetch64(data, pos + length - 8)
        c = _u64(_rotate(b, 37) * mul + a)
        d = _u64((_rotate(a, 25) + b) * mul)
        return _hash_len16_mul(c, d, mul)
    if length >= 4:
        mul = K2 + length * 2
        a = _fetch32(data, pos)
        return _hash_len16_mul(length + (a << 3), _fetch32(data, pos + length - 4), mul)
    if length > 0:
        a = data[pos]
        b = data[pos + (length >> 1)]
        c = data[pos + length - 1]
        y = a + (b << 8)
        z = length + (c << 2)
        return _u64(_shift_mix(y * K2 ^ z * K0) * K2)
    return K2


def _hash_len17to32(data: bytes, pos: int, length: int) -> int:
    mul = K2 + length * 2
    a = _u64(_fetch64(data, pos) * K1)
    b = _fetch64(data, pos + 8)
    c = _u64(_fetch64(data, pos + length - 8) * mul)
    d = _u64(_fetch64(data, pos + length - 16) * K2)
    return _hash_len16_mul(
        _rotate(a + b, 43) + _rotate(c, 30) + d,
        a + _rotate(b + K2, 18) + c,
        mul,
    )


def _hash_len33to64(data: bytes, pos: int, length: int) -> int:
    mul = K2 + length * 2
    a = _u64(_fetch64(data, pos) * K2)
    b = _fetch64(data, pos + 8)
    c = _fetch64(data, pos + length - 24)
    d = _fetch64(data, pos + length - 32)
    e = _u64(_fetch64(data, pos + 16) * mul)
    f = _u64(_fetch64(data, pos + 24) * 9)
    g = _fetch64(data, pos + length - 8)
    h = _u64(_fetch64(data, pos + length - 16) * mul)
    u = _u64(_rotate(a + g, 43) + (_rotate(b, 30) + c) * 9)
    v = _u64(((a + g) ^ d) + f + 1)
    w = _u64(_bswap64(_u64((u + v) * mul)) + h)
    x = _u64(_rotate(e + f, 42) + c)
    y = _u64((_bswap64(_u64((v + w) * mul)) + g) * mul)
    z = _u64(e + f + c)
    a = _u64(_bswap64(_u64((x + z) * mul + y)) + b)
    b = _u64(_shift_mix(_u64((z + a) * mul + d + h)) * mul)
    return _u64(b + x)


def _weak_hash_len32_with_seeds(data: bytes, pos: int, a: int, b: int) -> tuple[int, int]:
    w = _fetch64(data, pos)
    x = _fetch64(data, pos + 8)
    y = _fetch64(data, pos + 16)
    z = _fetch64(data, pos + 24)
    a = _u64(a + w)
    b = _rotate(b + a + z, 21)
    c = a
    a = _u64(a + x + y)
    b = _u64(b + _rotate(a, 44))
    return _u64(a + z), _u64(b + c)


def cityhash64(data: bytes) -> int:
    """Return CityHash64(data) as an unsigned 64-bit integer."""
    length = len(data)
    if length <= 32:
        if length <= 16:
            return _hash_len0to16(data, 0, length)
        return _hash_len17to32(data, 0, length)
    if length <= 64:
        return _hash_len33to64(data, 0, length)

    x = _fetch64(data, length - 40)
    y = _u64(_fetch64(data, length - 16) + _fetch64(data, length - 56))
    z = _hash_len16(_fetch64(data, length - 48) + length, _fetch64(data, length - 24))
    v_first, v_second = _weak_hash_len32_with_seeds(data, length - 64, length, z)
    w_first, w_second = _weak_hash_len32_with_seeds(data, length - 32, y + K1, x)
    x = _u64(x * K1 + _fetch64(data, 0))

    pos = 0
    remaining = (length - 1) & ~63
    while remaining:
        x = _u64(_rotate(x + y + v_first + _fetch64(data, pos + 8), 37) * K1)
        y = _u64(_rotate(y + v_second + _fetch64(data, pos + 48), 42) * K1)
        x ^= w_second
        y = _u64(y + v_first + _fetch64(data, pos + 40))
        z = _u64(_rotate(z + w_first, 33) * K1)
        v_first, v_second = _weak_hash_len32_with_seeds(data, pos, v_second * K1, x + w_first)
        w_first, w_second = _weak_hash_len32_with_seeds(
            data, pos + 32, z + w_second, y + _fetch64(data, pos + 16)
        )
        x, z = z, x
        pos += 64
        remaining -= 64

    return _hash_len16(
        _hash_len16(v_first, w_first) + _shift_mix(y) * K1 + z,
        _hash_len16(v_second, w_second) + x,
    )


def cityhash64_hex(data: bytes) -> str:
    return f"{cityhash64(data):016X}"
