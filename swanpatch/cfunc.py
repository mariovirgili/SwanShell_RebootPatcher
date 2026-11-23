# SPDX-License-Identifier: GPL-3.0-or-later
# SwanShellPatcher - Copyright (C) 2026 Mario Virgili
"""Method "cfunc": games written in C whose keypad function calls
inportb(0xB5) / outportb(0xB5, group) library functions.

Such a function is verbose: every inportb(0xB5) call is "push 0B5h / call
far inportb / pop cx" (9 bytes), every outportb(0xB5, g) is "push g / push
0B5h / call far outportb / add sp,4" (13 bytes), and the pointer to the
keys variable is loaded again and again (les bx,[bp-4]). The function is
rewritten in place:

  - those calls become "in al,0B5h" and "mov al,g / out 0B5h,al" (plus the
    "xor ah,ah" the library function does, if it does);
  - a "les bx,[bp+d]" that loads what ES:BX already holds is dropped;
  - the code after each change slides down; every jump, loop and near call
    is fixed for the new offsets;
  - the freed bytes, now in one block at the end of the function, hold a
    small routine (START + Y1 -> soft reset) called right after the read of
    the buttons group (40h).

No free space is needed and it works in banked code. The registers C
functions may change (BX, CX, DX, ES, flags) can end different; AX, SI, DI,
BP, DS, SS, the stack and RAM must end the same.
"""
import re

from .cpu import decode
from .rom import RESET, Geometry

_INB = re.compile(rb'\x55(?:\x8B\xEC|\x89\xE5)(\x56\x57)?\x8B\x56\x06\xEC(\x30\xE4|\x32\xE4)?'
                  rb'(?:\x5F\x5E)?\x5D\xCB')
_OUTB = re.compile(rb'\x55(?:\x8B\xEC|\x89\xE5)(\x56\x57)?\x8B\x56\x06\x8A\x46\x08\xEE'
                   rb'(\x30\xE4|\x32\xE4)?(?:\x5F\x5E)?\x5D\xCB')
_RET = (0xC2, 0xC3, 0xCA, 0xCB)


def _check_routine():
    """Called right after "in al, 0B5h" of the buttons group: AL = buttons.
    Keeps AX, CX and the flags; with START and Y1 down, soft reset."""
    w = b'\xB9\x18\x00\xE2\xFE'
    y = b'\x50\x51\xB0\x10\xE6\xB5' + w + b'\xE4\xB5\xA8\x01\xB0\x40\xE6\xB5' + w + b'\x59\x58'
    tail = y + bytes([0x75, 2]) + b'\x9D\xC3' + RESET
    return b'\x9C\xA8\x02' + bytes([0x74, len(y) + 2]) + tail


def _lib(data, geo):
    """{file offset: ('in'|'out', clears_ah)} of the library functions."""
    out = {}
    for rx, kind in ((_INB, 'in'), (_OUTB, 'out')):
        for m in rx.finditer(data):
            out[m.start()] = (kind, m.group(2) is not None)
    return out


def _target(geo, bank, off, seg):
    lin_seg = seg >> 12
    if lin_seg in (2, 3):
        return (bank << 16) | off if seg & 0x0FFF == 0 else None
    return geo.file_of(seg, off, bank)


class _Ins:
    __slots__ = ('off', 'raw', 'kind', 'new', 'target', 'rel')

    def __init__(self, off, raw, kind):
        self.off, self.raw, self.kind = off, raw, kind
        self.new = raw          # bytes to write (None = dropped)
        self.target = None      # file offset of a relative branch
        self.rel = None         # 'r8' / 'r16'


def _decode_function(data, start, limit=0x1000):
    """Instructions of the function at `start` (linear sweep) up to the
    return past every internal jump; None if something does not decode."""
    ins, p, far_end = [], start, start
    bank_base = start & ~0xFFFF
    while p < start + limit:
        d = decode(data, p)
        if d is None:
            return None
        n, kind = d
        it = _Ins(p, bytes(data[p:p + n]), kind)
        q = p
        while data[q] in (0x26, 0x2E, 0x36, 0x3E, 0xF0, 0xF2, 0xF3):
            q += 1
        op = data[q]
        if 0x70 <= op <= 0x7F or op in (0xE0, 0xE1, 0xE2, 0xE3, 0xEB):
            rel = data[q + 1] - 256 if data[q + 1] > 127 else data[q + 1]
            it.target = bank_base | ((p + n + rel) & 0xFFFF)
            it.rel = 'r8'
        elif op in (0xE8, 0xE9):
            it.target = bank_base | ((p + n + (data[q + 1] | data[q + 2] << 8)) & 0xFFFF)
            it.rel = 'r16'
        elif op in (0x0F, 0x62, 0x63, 0xD8, 0xD9, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF):
            return None
        ins.append(it)
        if it.target is not None and op != 0xE8 and it.target > far_end:
            far_end = it.target
        p += n
        if op in _RET and p > far_end:
            return ins
        if op in (0xE9, 0xEB, 0xEA) and p > far_end:
            return ins
    return None


def _function_start(data, site):
    """Start of the C function holding `site`: the nearest prologue
    (push bp / mov bp,sp, or enter n,0) right after a return."""
    lo = max((site & ~0xFFFF) + 3, site - 0x800)
    for p in range(site, lo, -1):
        prologue = data[p:p + 3] in (b'\x55\x8B\xEC', b'\x55\x89\xE5') or \
            (data[p] == 0xC8 and data[p + 3] == 0x00)
        after_ret = data[p - 1] in (0xC3, 0xCB, 0x90, 0x00, 0xFF) or data[p - 3] in (0xC2, 0xCA)
        if prologue and after_ret:
            return p
    return None


def _uses_bx_es(it):
    """Conservative: may this instruction change BX, ES or the pointer?"""
    b = it.raw
    q = 0
    while b[q] in (0x26, 0x2E, 0x36, 0x3E, 0xF0, 0xF2, 0xF3):
        q += 1
    op = b[q]
    if op in (0x07, 0x43, 0x4B, 0x53, 0x5B, 0x87, 0x93, 0xBB, 0xC4, 0xC5, 0x61, 0xCF,
              0xCA, 0xCB, 0xC2, 0xC3, 0x9A, 0xE8, 0xCD, 0xCC):
        return True
    if op == 0x8E and (b[q + 1] >> 3) & 7 == 0:
        return True
    if op == 0xFF and (b[q + 1] >> 3) & 7 in (2, 3):
        return True
    return False


def _writes_mem_or_bx(it):
    b = it.raw
    q = 0
    while b[q] in (0x26, 0x2E, 0x36, 0x3E, 0xF0, 0xF2, 0xF3):
        q += 1
    op = b[q]
    # writes through a ModRM destination (bx as register, or any memory)
    if op in (0x00, 0x01, 0x08, 0x09, 0x10, 0x11, 0x18, 0x19, 0x20, 0x21, 0x28, 0x29,
              0x30, 0x31, 0x86, 0x87, 0x88, 0x89, 0x8C, 0x8F, 0xC6, 0xC7, 0xD0, 0xD1, 0xD2,
              0xD3, 0xC0, 0xC1, 0xFE, 0xFF, 0x80, 0x81, 0x83, 0xF6, 0xF7):
        m = b[q + 1]
        if m >> 6 != 3:
            return True                        # a memory write: may touch the pointer
        if m & 7 == 3:
            return True                        # BX / BL changed
    if op in (0x02, 0x03, 0x0A, 0x0B, 0x12, 0x13, 0x1A, 0x1B, 0x22, 0x23, 0x2A, 0x2B,
              0x32, 0x33, 0x8A, 0x8B, 0x8D):
        if (b[q + 1] >> 3) & 7 == 3:
            return True                        # BX / BL is the destination
    if op in (0xA4, 0xA5, 0xAA, 0xAB, 0xA2, 0xA3):
        return True
    return False


def cfunc(data):
    geo = Geometry(data)
    lib = _lib(data, geo)
    if not lib:
        return None, 'no inportb/outportb functions'
    work = bytearray(data)
    notes, tests = [], []
    done_funcs = set()
    # every "push 0B5h / call far inportb" in the ROM
    for m in re.finditer(rb'(?:\x68\xB5\x00|\x6A\xB5)(?:\x9A(..)(..)|\x0E\xE8(..))', data, re.S):
        call = m.start() + (3 if data[m.start()] == 0x68 else 2)
        bank = call >> 16
        if m.group(1) is not None:
            t = _target(geo, bank, int.from_bytes(m.group(1), 'little'),
                        int.from_bytes(m.group(2), 'little'))
        else:
            t = (bank << 16) | ((call + 4 + int.from_bytes(m.group(3), 'little')) & 0xFFFF)
        if t is None or lib.get(t, ('',))[0] != 'in':
            continue
        start = _function_start(data, m.start())
        if start is None or start in done_funcs:
            continue
        done_funcs.add(start)
        got = _rewrite(data, geo, lib, start, bank)
        if got is None:
            continue
        new_bytes, sel_last = got
        work[start:start + len(new_bytes)] = new_bytes
        notes.append('%06X' % start)
        tests.append(dict(name='cfunc', start=start, sel=sel_last, stack=(0x0100, 0x0000),
                          ignore=('FLAGS', 'BX', 'CX', 'DX', 'ES'), cs=geo.seg(bank)))
    if not notes:
        return None, 'no C keypad function that can be rewritten'
    return bytes(work), notes, tests


def _call(ins, k, n, geo, bank, targets):
    """(library function, instructions used) for a call at ins[k]: "call
    far" or "push cs / call near"; (None, 0) otherwise."""
    if k < n and ins[k].raw[0] == 0x9A and ins[k].off not in targets:
        c = ins[k].raw
        return _target(geo, bank, c[1] | c[2] << 8, c[3] | c[4] << 8), 1
    if k + 1 < n and ins[k].raw == b'\x0E' and ins[k + 1].raw[0] == 0xE8 \
            and ins[k].off not in targets and ins[k + 1].off not in targets:
        return ins[k + 1].target, 2
    return None, 0


def _rewrite(data, geo, lib, start, bank):
    ins = _decode_function(data, start)
    if not ins:
        return None
    end = ins[-1].off + len(ins[-1].raw)
    offs = {it.off for it in ins}
    targets = {it.target for it in ins if it.target is not None and start <= it.target < end}
    if not targets <= offs:
        return None                                   # jump into the middle of an instruction
    group, sel_last, read40 = None, 0x10, None
    inline_in = []
    i = 0
    n = len(ins)
    while i < n:
        it = ins[i]
        r = it.raw
        # empty delay loop on a byte local: for (i = 0; i < N; i++);
        #   mov byte [bp+d],0 / jmp cmp / (nop) / inc byte [bp+d] /
        #   cmp: cmp byte [bp+d],N / jl inc
        # -> mov byte [bp+d],N / push cx / mov cx,3N / loop $ / pop cx
        if len(r) == 4 and r[:2] == b'\xC6\x46' and r[3] == 0 and i + 4 < n:
            d = r[2]
            k = i + 1
            grp = [it]
            if ins[k].raw[0] == 0xEB:
                jmp = ins[k]
                grp.append(jmp)
                k += 1
                if ins[k].raw == b'\x90':
                    grp.append(ins[k])
                    k += 1
                if k + 2 < n and ins[k].raw == bytes([0xFE, 0x46, d]) \
                        and ins[k + 1].raw[:3] == bytes([0x80, 0x7E, d]) \
                        and ins[k + 2].raw[0] == 0x7C:
                    inc, cmp_, jl = ins[k], ins[k + 1], ins[k + 2]
                    grp += [inc, cmp_, jl]
                    members = {g.off for g in grp}
                    outside = {x.target for x in ins if x.target is not None and x.off not in members}
                    if jmp.target == cmp_.off and jl.target in (grp[2].off, inc.off) \
                            and not (outside & (members - {it.off})):
                        nn = cmp_.raw[3]
                        it.new = bytes([0xC6, 0x46, d, nn, 0x51, 0xB9]) + \
                            ((nn * 3) or 1).to_bytes(2, 'little') + b'\xE2\xFE\x59'
                        for g in grp[1:]:
                            g.new = None
                        i += len(grp)
                        continue
        # push 0B5h / call far inportb / pop cx
        t, used = _call(ins, i + 1, n, geo, bank, targets) if r in (b'\x68\xB5\x00', b'\x6A\xB5') \
            else (None, 0)
        if used and i + used + 1 < n and ins[i + used + 1].raw == b'\x59' \
                and ins[i + used + 1].off not in targets and t in lib and lib[t][0] == 'in':
            it.new = b'\xE4\xB5' + (b'\x32\xE4' if lib[t][1] else b'')
            for k in range(1, used + 2):
                ins[i + k].new = None
            inline_in.append(it)
            if group == 0x40:
                read40 = it
            i += used + 2
            continue
        # push g / push 0B5h / call far outportb / add sp,4
        if r[:1] == b'\x6A' and i + 2 < n and ins[i + 1].raw in (b'\x68\xB5\x00', b'\x6A\xB5') \
                and ins[i + 1].off not in targets:
            t, used = _call(ins, i + 2, n, geo, bank, targets)
            k = i + 2 + used
            if used and k < n and ins[k].raw == b'\x83\xC4\x04' and ins[k].off not in targets \
                    and t in lib and lib[t][0] == 'out':
                group = r[1]
                sel_last = group & 0x70
                it.new = bytes([0xB0, r[1], 0xE6, 0xB5]) + (b'\x32\xE4' if lib[t][1] else b'')
                for j in range(i + 1, k + 1):
                    ins[j].new = None
                i = k + 1
                continue
        i += 1
    if read40 is None:
        return None
    # "xor ah,ah" of an inline read is not needed when the next instruction
    # sets AH again (mov ah,0 / xor ah,ah / another read that clears it)
    kept = [x for x in ins if x.new is not None]
    for a, b in zip(kept, kept[1:]):
        if a in inline_in and a.new.endswith(b'\x32\xE4') and b.off not in targets and (
                b.raw[:2] in (b'\xB4\x00', b'\x32\xE4', b'\x30\xE4') or
                (b in inline_in and b.new.endswith(b'\x32\xE4'))):
            a.new = a.new[:-2]
    # drop "les bx,[bp+d]" that reload what ES:BX already holds
    known = None
    for it in ins:
        if it.off in targets:
            known = None
        if it.new is None:
            continue
        if len(it.raw) == 3 and it.raw[0] == 0xC4 and it.raw[1] == 0x5E:
            if known == it.raw[2]:
                it.new = None
                continue
            known = it.raw[2]
            continue
        if _uses_bx_es(it) or _writes_mem_or_bx(it) or it.target is not None:
            known = None
    # the check routine is called right after the read of the buttons group
    read40.new = read40.new + b'\xE8\x00\x00'
    chk = _check_routine()
    # new layout
    new_off, pos = {}, start
    for it in ins:
        new_off[it.off] = pos
        if it.new is not None:
            pos += len(it.new)
    body_end = pos
    if body_end + len(chk) > end:
        return None
    out = bytearray()
    for it in ins:
        if it.new is None:
            continue
        b = bytearray(it.new)
        here = new_off[it.off]
        if it.rel and it.new == it.raw:
            q = 0
            while b[q] in (0x26, 0x2E, 0x36, 0x3E, 0xF0, 0xF2, 0xF3):
                q += 1
            t = it.target
            nt = new_off.get(t, t) if start <= t < end else t
            if it.rel == 'r8':
                d = nt - (here + len(b))
                if not -128 <= d <= 127:
                    return None
                b[q + 1] = d & 0xFF
            else:
                d = nt - (here + len(b))
                b[q + 1:q + 3] = (d & 0xFFFF).to_bytes(2, 'little')
        if it is read40:
            at = here + len(b) - 3
            d = body_end - (at + 3)
            b[-2:] = (d & 0xFFFF).to_bytes(2, 'little')
        out += b
    out += chk
    out += b'\x90' * (end - start - len(out))
    return bytes(out), sel_last
