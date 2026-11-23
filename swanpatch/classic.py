# SPDX-License-Identifier: GPL-3.0-or-later
# SwanShellPatcher - Copyright (C) 2026 Mario Virgili
"""The classic patch: the same search and the same code as the SwanShellDX
menu does at launch (src/menu/launch/launch_patch.c), from y1p.py.

A "classic" button read is "mov al, 40h / out 0B5h, al", a short wait and
"in al, 0B5h". The wait is replaced by a call to the hook (85 bytes, see
rom.HOOK), which reads the buttons for the game and resets on START + Y1.

extended=True: same reads, but the hook may also go in any free space
found by rom.Space (the menu only uses FFh at the end of a bank)."""
import re

from .rom import HOOK, HOOK_PLAIN, HOOK_RET1, HOOK_RET2, Space, Geometry

PLAIN, PLAIN_CX0, SELECT, SELECT_CX0, LOOP = 'nop', 'nop-cx0', 'select', 'select-cx0', 'loop'


def find_select(bank, start):
    """Next "mov al, 40h / out 0B5h, al" (B0 40 E6 B5) from start, below FF00h."""
    i = bank.find(b'\xB0\x40\xE6\xB5', start)
    return -1 if i < 0 or i >= 0xFF00 else i


def read_site(bank, i):
    """(form, region start, offset of the IN) of the button read whose
    "mov al, 40h" is at i, or None.
      loop:       ... mov cx,n / (nop) / loop / pop cx / in   -> from the mov cx
      select:     out / nop... / in                           -> from the out
      select-cx0: out / mov cx,n / loop $ / nop... / in       -> from the out
      nop-cx0:    out / code / mov cx,n / loop $ / nop... / in -> from the mov cx
      nop:        out / other code / nop... / in              -> from the NOPs"""
    si = i + 4
    inp = None
    for _ in range(96):
        if si >= 0x10000:
            break
        if bank[si] == 0xB5:
            p = bank[si - 1]
            if p == 0xE4:
                inp = si - 1
                break
            if p in (0xE6, 0xE7):
                break
        si += 1
    if inp is None:
        return None
    if bank[inp - 1] == 0x59:
        if bank[inp - 3] != 0xE2:
            return None
        d = bank[inp - 2]
        if d == 0xFE and bank[inp - 6] == 0xB9:
            return LOOP, inp - 6, inp
        if d == 0xFD and bank[inp - 4] == 0x90 and bank[inp - 7] == 0xB9:
            return LOOP, inp - 7, inp
        if d == 0xFC and bank[inp - 5] == 0x90 and bank[inp - 4] == 0x90 and bank[inp - 8] == 0xB9:
            return LOOP, inp - 8, inp
        return None
    n = 0
    while n < 255 and bank[inp - 1 - n] == 0x90:
        n += 1
    k = inp - n                                # first NOP (or the IN)
    if k == i + 4:
        return SELECT, i + 2, inp
    if k - 5 >= i + 4 and bank[k - 5] == 0xB9 and bank[k - 2] == 0xE2 and bank[k - 1] == 0xFE:
        # "mov cx, n / loop $" right before the NOPs
        if k - 5 == i + 4:
            return SELECT_CX0, i + 2, inp
        return PLAIN_CX0, k - 5, inp
    if n >= 1:
        return PLAIN, k, inp
    return None


def free_place(bank, size, last_bank=False):
    """Offset for the hook: the last run of FFh below FF00h that reaches
    FF00h or is 1 KB or more; else (not in the ROM's last bank) a run of 00h
    padding up to the end of the bank. The hook goes 64 bytes before the end
    of the run (FF00h at most)."""
    need = max(size + 128, 256)
    best_end = None
    for m in re.finditer(rb'\xFF+', bank[:0xFF00]):
        length = m.end() - m.start()
        if length >= need and (m.end() == 0xFF00 or length >= 1024):
            best_end = m.end()
    if best_end is None and not last_bank:
        m = re.search(rb'\x00+\Z', bank)
        if m and len(bank) == 0x10000 and 0xFF00 - m.start() >= need:
            best_end = 0xFF00
    return None if best_end is None else best_end - size - 64


def auto_patch(data, deep=False, extended=False, caves=False):
    """The ROM as the menu would patch it, or (None, reason).
    deep: when the last 16 banks have no classic read, also look in the
    lower (banked-only) banks; the menu never does this (too slow at launch),
    so those games need their .Y1P."""
    total = (len(data) + 0xFFFF) >> 16
    first = max(0, total - 16)

    def linear(b):                  # seen in the linear window (segments 4-F)
        return b >= first and (total < 16 or ((b - total) & 0x0F) >= 4)

    def scan(banks):
        found = []
        for b in banks:
            bank = data[b << 16:(b + 1) << 16]
            i = find_select(bank, 8)
            while i >= 0:
                site = read_site(bank, i)
                if site:
                    found.append((b,) + site)
                i = find_select(bank, i + 1)
        return found

    sites = scan(range(first, total))
    if not sites and deep:
        sites = scan(range(0, first))
    if len(sites) > 2:
        return None, 'more than two button reads'
    if not sites:
        return None, 'no classic button read'
    work = bytearray(data)
    space = Space(work, Geometry(data), caves)
    done = 0
    placed = []                         # hooks already copied: (bank, offset, far)

    def get_hook(b, far):
        """(bank, offset) of a hook this call can reach: one already copied
        (any far one for a far call, one in the same bank for a near call),
        else a new copy in the read's bank (near) or in a linear bank (far)."""
        for pb, ph, pf in placed:
            if pf == far and (far or pb == b):
                return pb, ph
        if far:
            banks = ([b] if linear(b) else []) + \
                [x for x in range(total - 1, first - 1, -1) if x != b and linear(x)]
        else:
            banks = [b]
        for x in banks:
            hook = free_place(bytes(work[x << 16:(x + 1) << 16]), len(HOOK), x == total - 1)
            if hook is None and extended:
                got = space.alloc(len(HOOK), [x])
                hook = None if got is None else got & 0xFFFF
            if hook is not None:
                blob = bytearray(HOOK)
                if not far:
                    blob[HOOK_RET1] = blob[HOOK_RET2] = 0xC3
                work[(x << 16) + hook:(x << 16) + hook + len(blob)] = blob
                placed.append((x, hook, far))
                return x, hook
        return None

    for b, form, start, inp in reversed(sites):
        room = inp + 2 - start
        extra = 1 if form == LOOP else 3 if form in (SELECT_CX0, PLAIN_CX0) else 0
        # linear bank: far call. Banked window (2000h/3000h): near call to a
        # hook in the same bank, else far call to a hook in a linear bank
        # (the linear window does not move while the game runs).
        got = None
        far = linear(b)
        if not far and room >= 3 + extra:
            got = get_hook(b, False)
        if got is None and room >= 5 + extra:
            far = True
            got = get_hook(b, True)
        if got is None:
            continue
        hb, hook = got
        entry = hook + (0 if form in (SELECT, SELECT_CX0) else HOOK_PLAIN)
        new = bytearray()
        if form == LOOP:
            new.append(0x59)                   # pop cx (the loop goes)
        pos = start + len(new)
        if far:
            seg = ((hb - total) & 0x0F) << 12
            new += bytes([0x9A]) + entry.to_bytes(2, 'little') + seg.to_bytes(2, 'little')
        else:
            new += bytes([0xE8]) + ((entry - (pos + 3)) & 0xFFFF).to_bytes(2, 'little')
        if form in (SELECT_CX0, PLAIN_CX0):
            new += b'\xB9\x00\x00'             # mov cx, 0: as after the game's loop
        new += b'\x90' * (room - len(new))
        work[(b << 16) + start:(b << 16) + start + room] = new
        done += 1
    if not done:
        return None, 'no room for the call or the hook'
    return bytes(work), 'ok'


