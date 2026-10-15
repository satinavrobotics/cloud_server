"""Compact encodings for the viewer's data: base64 arrays, run-length rows and sparse per-capture changes.

Everything here is pure (numpy only) so it can be tested without the engine.
"""
import base64

import numpy as np


def b64(a):
    """base64 of a contiguous array's bytes"""
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def rle(a):
    """run-length encoding of a flat uint8 array as base64 (value, run) byte pairs; a run is at most 255 long"""
    flat = np.ascontiguousarray(a, dtype=np.uint8).ravel()
    if flat.size == 0:
        return ""
    # boundaries of equal runs, then split every run into pieces of at most 255
    edges = np.flatnonzero(np.diff(flat)) + 1
    starts = np.concatenate(([0], edges))
    lengths = np.diff(np.concatenate((starts, [flat.size])))
    out = bytearray()
    for v, start, length in zip(flat[starts], starts, lengths):
        left = int(length)
        while left > 0:
            run = min(left, 255)
            out += bytes((int(v), run))
            left -= run
    return base64.b64encode(bytes(out)).decode()


def varint(n, out):
    """append the unsigned LEB128 encoding of n to the bytearray out"""
    if n < 0:
        raise ValueError("varint takes a non-negative integer")
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def delta(prev_a, prev_b, a, b):
    """sparse change of two uint8 planes (flat, same length) against the previous plane: base64 of the varint count, then per
    changed cell the varint gap from the previous changed cell, its value in plane a and in plane b"""
    changed = np.flatnonzero((a != prev_a) | (b != prev_b))
    out = bytearray()
    varint(len(changed), out)
    last = -1
    for k in changed:
        varint(int(k) - last - 1, out)
        out += bytes((int(a[k]), int(b[k])))
        last = int(k)
    return base64.b64encode(bytes(out)).decode()


def unrle(b64_text, n):
    """inverse of rle: a flat uint8 array of length n"""
    raw = base64.b64decode(b64_text) if b64_text else b""
    pairs = np.frombuffer(raw, np.uint8).reshape(-1, 2) if raw else np.zeros((0, 2), np.uint8)
    out = np.repeat(pairs[:, 0], pairs[:, 1].astype(np.int64))
    if out.size != n:
        raise ValueError("run-length data has %d cells, expected %d" % (out.size, n))
    return out


def apply_delta(text, a, b):
    """apply one delta (see delta) to the uint8 planes a and b in place"""
    raw = base64.b64decode(text) if text else b""
    pos, count, idx = 0, 0, -1
    count, pos = _read_varint(raw, pos)
    for _ in range(count):
        gap, pos = _read_varint(raw, pos)
        idx += gap + 1
        a[idx] = raw[pos]
        b[idx] = raw[pos + 1]
        pos += 2


def _read_varint(raw, pos):
    n, shift = 0, 0
    while True:
        byte = raw[pos]
        pos += 1
        n |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return n, pos
