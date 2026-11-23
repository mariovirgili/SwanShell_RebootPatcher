# SPDX-License-Identifier: GPL-3.0-or-later
# SwanShellPatcher - Copyright (C) 2026 Mario Virgili
"""ROM identity, memory map, free space and patch files (.Y1P, IPS).

Everything here is plain Python 3 (standard library only).
"""
import os
import re
import zlib

# --------------------------------------------------------------------------
# The hook (SwanShellDX menu: src/menu/launch/launch_patch.c, kr_hook).
# Entry 00h ("select"): selects the buttons group (out 0B5h, 40h), waits,
# then goes on as entry 0Bh ("plain"): in al, 0B5h for the game; with START
# + Y1 down, soft reset into the menu. Returns far (RETF at 13h and 34h);
# for a near call those two bytes become RET (C3h).
HOOK = bytes.fromhex(
    "51 b0 40 e6 b5 b9 18 00 e2 fe 59 9c e4 b5 a8 02 75 02 9d cb 50 51 b0 10"
    " e6 b5 b9 18 00 e2 fe e4 b5 88 c4 b0 40 e6 b5 b9 18 00 e2 fe f6 c4 01 75"
    " 04 59 58 9d cb fa 31 c0 e6 b7 b0 dd e6 e2 31 c0 e7 00 48 50 40 50 48 68"
    " 90 ea 68 e7 e4 68 e6 c0 89 e1 16 51 cb")
assert len(HOOK) == 85
HOOK_PLAIN, HOOK_RET1, HOOK_RET2 = 0x0B, 0x13, 0x34

# The soft reset alone (the hook's tail, 32 bytes): interrupts off, unlock
# the nileswan power register (E2h = DDh), display off, then from a few bytes
# of code written on the stack: ROM linear bank FFh, bank mask, JMP FFFF:0000
# (the IPL, which starts the menu again). Works from any bank or RAM.
RESET = HOOK[0x35:]
assert RESET[0] == 0xFA and len(RESET) == 32

ROM_EXTS = ('.ws', '.wsc', '.pc2')

# Keypad (port B5h): write the group in bits 4-6 (10h = Y1-Y4, 20h = X1-X4,
# 40h = START/A/B), read the four keys in bits 0-3 (bits 4-6 read back the
# group). Y1 = bit 0 of group 10h, START = bit 1 of group 40h.
KEYPAD = 0xB5


# === identity ===

# ROM size code of the header (footer byte 10) -> 64 KB banks, as the
# menu reads it (src/menu/launch/launch.c, rom_banks[]); 0 when unknown
HEADER_BANKS = (2, 4, 8, 16, 32, 48, 64, 96, 128, 256, 512, 1024)


def rom_key(data):
    """<publisher><game id><version><checksum><banks>, the .Y1P name. The
    banks come from the header's ROM size code, as SwanShellDX reads it -
    not from the file size: some ROMs (translations, homebrew) declare
    another size, and the menu would look for another file."""
    f = data[-16:]
    publisher, game_id, version = f[6], f[8], f[9]
    checksum = f[14] | (f[15] << 8)
    banks = HEADER_BANKS[f[10]] if f[10] < len(HEADER_BANKS) else 0
    return '%02X%02X%02X%04X%04X' % (publisher, game_id, version, checksum, banks)


def file_key(data):
    """The key with the banks of the file size (what y1p.py and SwanShellPatcher
    1.0-1.2 used): looked up too, for .Y1P files made that way."""
    f = data[-16:]
    return '%02X%02X%02X%04X%04X' % (f[6], f[8], f[9], f[14] | (f[15] << 8),
                                     (len(data) + 0xFFFF) >> 16)


def rom_crc(data):
    return '%08X' % (zlib.crc32(data) & 0xFFFFFFFF)


def has_header(data):
    """A WonderSwan ROM starts at FFFF:0000 with a far jump (EAh)."""
    return len(data) >= 16 and data[-16] == 0xEA


# === memory map ===

class Geometry:
    """Where each 64 KB bank of the ROM shows up in the CPU's 1 MB space.

    Segments 4000h-F000h: the "linear" window, the last 16 banks (bank n at
    segment ((n - banks) & 15) << 12), fixed while the game runs.
    Segments 2000h and 3000h: banked windows, any bank (ports C2h/C3h);
    code there can only call code in the same bank or in the linear window."""

    def __init__(self, data):
        self.size = len(data)
        self.banks = (len(data) + 0xFFFF) >> 16
        self.first = max(0, self.banks - 16)

    def linear(self, b):
        return b >= self.first and (self.banks < 16 or ((b - self.banks) & 0x0F) >= 4)

    def seg(self, b):
        """Segment a bank is seen at: its linear segment, else 2000h."""
        return ((b - self.banks) & 0x0F) << 12 if self.linear(b) else 0x2000

    def far(self, offset):
        """(offset, segment) for a far call to a file offset in a linear bank."""
        b = offset >> 16
        assert self.linear(b), 'bank %02X is not in the linear window' % b
        return offset & 0xFFFF, self.seg(b)

    def file_of(self, seg, off, bank=None):
        """File offset of seg:off (linear window; `bank` for 2000h/3000h)."""
        lin = ((seg << 4) + off) & 0xFFFFF
        s = lin >> 16
        if s in (2, 3):
            return None if bank is None else (bank << 16) | (lin & 0xFFFF)
        if s < 4:
            return None
        for b in range(self.first, self.banks):
            if self.linear(b) and self.seg(b) >> 12 == s:
                return (b << 16) | (lin & 0xFFFF)
        return None

    def linear_banks(self, prefer=None):
        order = [b for b in range(self.banks - 1, self.first - 1, -1) if self.linear(b)]
        if prefer is not None and prefer in order:
            order.remove(prefer)
            order.insert(0, prefer)
        return order


def far_call(geo, target):
    off, seg = geo.far(target)
    return bytes([0x9A]) + off.to_bytes(2, 'little') + seg.to_bytes(2, 'little')


def far_jump(geo, target):
    off, seg = geo.far(target)
    return bytes([0xEA]) + off.to_bytes(2, 'little') + seg.to_bytes(2, 'little')


def near_rel(op, at, target):
    """E8/E9 rel16 placed at `at` (same bank) to `target`."""
    return bytes([op]) + (((target & 0xFFFF) - ((at & 0xFFFF) + 3)) & 0xFFFF).to_bytes(2, 'little')


# === free space ===

class Space:
    """Hands out free space for new code, never twice the same bytes.

    strict (default): runs of FFh reaching the end of a bank (below the
    header in the last bank) or 1 KB long, and 00h padding at the end of a
    bank - what is left over by the build tools. caves=True also accepts
    long runs of 00h inside a bank: they may be graphics or tables, so the
    result must be tested on the console."""

    def __init__(self, work, geo, caves=False):
        self.work, self.geo, self.caves = work, geo, caves
        self.used = []
        self.kind = 'FF'

    def _free(self, a, n):
        return all(not (a < e and s < a + n) for s, e in self.used)

    def runs(self, b):
        bank = bytes(self.work[b << 16:(b + 1) << 16])
        top = 0xFFF0 if b == self.geo.banks - 1 else 0x10000
        out = []
        for m in re.finditer(rb'\xFF{64,}', bank[:top]):
            if m.end() >= top - 0x100 or m.end() - m.start() >= 1024:
                out.append((m.start(), m.end(), 'FF'))
        m = re.search(rb'\x00{64,}\Z', bank[:top])
        if m:
            out.append((m.start(), m.end(), '00 tail'))
        if self.caves:
            for m in re.finditer(rb'\x00{256,}', bank[:top]):
                if m.end() < top:
                    out.append((m.start(), m.end(), '00 cave'))
        return out

    def alloc(self, size, banks, margin=32):
        """File offset of `size` free bytes in one of `banks` (in order), or
        None. Taken from the end of a run, `margin` bytes before it."""
        for b in banks:
            for s, e, kind in sorted(self.runs(b), key=lambda r: r[2] != 'FF'):
                end = e - margin
                start = end - size
                if start < s + 16:
                    continue
                a = (b << 16) + start
                while a >= (b << 16) + s + 16 and not self._free(a, size):
                    a -= size
                if a < (b << 16) + s + 16:
                    continue
                self.used.append((a, a + size))
                self.kind = kind
                return a
        return None


# === patch files ===

def diff_records(old, new, maxlen=160):
    recs, i, n = [], 0, len(old)
    while i < n:
        if new[i] != old[i]:
            j = i
            while j < n and new[j] != old[j] and j - i < maxlen:
                j += 1
            recs.append((i, bytes(old[i:j]), bytes(new[i:j])))
            i = j
        else:
            i += 1
    return recs


def read_y1p(path):
    """[(name, crc, [(offset, original, new)])]"""
    out = []
    if not os.path.exists(path):
        return out
    cur = None
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.strip()
        if not line or line.startswith(';'):
            continue
        if line.startswith('[') and line.endswith(']'):
            cur = [line[1:-1], '', []]
            out.append(cur)
        elif cur is not None and line.startswith('crc='):
            cur[1] = line[4:].upper()
        elif cur is not None and line.startswith('p='):
            o, a, b = line[2:].split(':')
            cur[2].append((int(o, 16), bytes.fromhex(a), bytes.fromhex(b)))
    return out


def write_y1p(path, variants, comment='SwanShellDX START+Y1 patch: offset:original:new (hex)'):
    with open(path, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write('; %s\n' % comment)
        for name, crc, recs in variants:
            f.write('[%s]\n' % name.replace('[', '(').replace(']', ')'))
            if crc:
                f.write('crc=%s\n' % crc)
            for o, a, b in recs:
                f.write('p=%06X:%s:%s\n' % (o, a.hex().upper(), b.hex().upper()))


def matches(data, recs):
    return bool(recs) and all(data[o:o + len(a)] == a for o, a, _ in recs)


def apply_records(data, recs):
    out = bytearray(data)
    for o, _, b in recs:
        out[o:o + len(b)] = b
    return bytes(out)


def save_variant(outdir, rom_name, data, records, method):
    """Adds (or replaces, same CRC) this ROM's variant in <key>.Y1P and its
    line in PATCHES.TXT. Returns the .Y1P path."""
    os.makedirs(outdir, exist_ok=True)
    key, crc = rom_key(data), rom_crc(data)
    for o, a, b in records:
        if len(a) != len(b) or len(b) > 160:
            raise ValueError('record at %06X: bad length' % o)
        if data[o:o + len(a)] != a:
            raise ValueError('record at %06X: original bytes do not match the ROM' % o)
    name = os.path.splitext(os.path.basename(rom_name))[0]
    path = os.path.join(outdir, key + '.Y1P')
    variants = [v for v in read_y1p(path) if v[1] != crc]
    variants.append([name, crc, records])
    write_y1p(path, variants)
    listing = os.path.join(outdir, 'PATCHES.TXT')
    lines = []
    if os.path.exists(listing):
        lines = [l.rstrip('\r\n') for l in open(listing, encoding='utf-8', errors='replace')
                 if not l.startswith(crc + ';') and not l.startswith('CRC32;') and l.strip()]
    lines.insert(0, 'CRC32;size;key;form;records;ROM')
    lines.append('%s;%d;%s;%s;%d;%s' % (crc, len(data), key, method, len(records),
                                        os.path.basename(rom_name)))
    with open(listing, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write('\n'.join(lines) + '\n')
    return path


def ips_records(data, ips):
    if ips[:5] != b'PATCH':
        raise ValueError('not an IPS file')
    new = bytearray(data)
    i = 5
    while ips[i:i + 3] != b'EOF':
        o = int.from_bytes(ips[i:i + 3], 'big')
        n = int.from_bytes(ips[i + 3:i + 5], 'big')
        i += 5
        if n:
            chunk = ips[i:i + n]
            i += n
        else:
            run = int.from_bytes(ips[i:i + 2], 'big')
            chunk = bytes([ips[i + 2]]) * run
            i += 3
        if o + len(chunk) > len(new):
            raise ValueError('IPS writes past the end of the ROM (expanded ROMs are not supported)')
        new[o:o + len(chunk)] = chunk
    return diff_records(data, bytes(new))


def make_ips(records):
    out = bytearray(b'PATCH')
    for o, _, b in records:
        out += o.to_bytes(3, 'big') + len(b).to_bytes(2, 'big') + b
    out += b'EOF'
    return bytes(out)
