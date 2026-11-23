"""The ways a START + Y1 reset can be put into a WonderSwan game.

Every method takes the ROM (bytes) and returns a list of Candidate: the
patched ROM, the method name, notes, and the tests that check it (see
emu.py; None = the standard tests on the patched button reads).

    database    a .Y1P already made for this ROM (the patches/ folder)
    classic     the SwanShellDX menu's own patch (same code, same places)
    classic+    the same button reads, with the hook in any free space
    trampoline  reads the menu does not know (DAA delay, no delay, code
                after the read...): the read and the instructions around it
                move to a small routine in free space that calls the hook
    port        a patch made for another version of the game (translation,
                revision, expanded ROM): the same code found at another
                offset, the new code moved if its place is no longer free
    inportb     games in C that read the keys with a function
                inportb(port) (mov dx,[bp+6] / in al,dx): the function jumps
                to a copy that also checks START + Y1 on port 0B5h
    nopsled     a long run of NOPs used as a delay before the read: the check
                and the reset are written in the NOPs (no free space needed,
                works in banked code)
    cblock      C code that copies the keys bit by bit (test / je / or):
                the block is rewritten in a few bytes, the check and the
                reset go in the bytes it frees
    anyread     any other keypad read (any group, in al,dx, rep in...): the
                read moves to a small routine in free space that first runs
                the full check (select 40h, START + Y1) and selects the
                game's group again
    vblank      games that never read the keys (demos): the check goes in
                the VBlank interrupt handler
"""
import os
import re

from . import classic as _classic
from .rom import (HOOK, HOOK_PLAIN, RESET, Geometry, Space, far_call, far_jump, rom_key,
                  read_y1p, matches, apply_records, diff_records)


class Candidate:
    def __init__(self, method, data, note='', tests=None, risk=''):
        self.method, self.data, self.note, self.tests, self.risk = method, data, note, tests, risk


def _w(v):
    return (v & 0xFFFF).to_bytes(2, 'little')


def _jcc(op, dist):
    assert 0 <= dist < 128, dist
    return bytes([op, dist])


def keypad_free(data):
    """True when nothing in the ROM looks like a keypad access."""
    pats = (b'\xE4\xB5', b'\xBA\xB5\x00', b'\x68\xB5\x00', b'\x6A\xB5', b'\xB2\xB5', b'\xE5\xB4')
    return not any(p in data for p in pats)


# ------------------------------------------------------------------ database

def database(data, db_dirs):
    key = rom_key(data)
    out = []
    for d in db_dirs:
        for name, crc, recs in read_y1p(os.path.join(d, key + '.Y1P')):
            if matches(data, recs):
                out.append(Candidate('database', apply_records(data, recs),
                                     'variant [%s] of %s.Y1P' % (name, key)))
                return out
    return out


# ------------------------------------------------------------------ classic

def classic(data):
    new, why = _classic.auto_patch(data, deep=False)
    if new is None:
        new, why2 = _classic.auto_patch(data, deep=True)
        if new is None:
            return [], why
        return [Candidate('classic-deep', new, 'classic read in a banked bank')], ''
    return [Candidate('classic', new, 'same patch as the menu at launch')], ''


def classic_plus(data, caves=False):
    new, why = _classic.auto_patch(data, deep=True, extended=True, caves=caves)
    if new is None:
        return [], why
    return [Candidate('classic+', new, 'classic read, hook in extra free space')], ''


# ------------------------------------------------------------------ trampoline

_ALU8 = {0x04, 0x0C, 0x24, 0x2C, 0x34, 0x3C, 0xA8}
_ALU16 = {0x05, 0x0D, 0x25, 0x2D, 0x35, 0x3D, 0xA9}
_REGREG = {0x00, 0x01, 0x02, 0x03, 0x08, 0x09, 0x0A, 0x0B, 0x20, 0x21, 0x22, 0x23, 0x28,
           0x29, 0x2A, 0x2B, 0x30, 0x31, 0x32, 0x33, 0x38, 0x39, 0x3A, 0x3B, 0x84, 0x85,
           0x88, 0x89, 0x8A, 0x8B, 0xD0, 0xD1, 0xD2, 0xD3}


def ilen(b, i):
    """Length of a simple, position-independent instruction at b[i] (no
    memory operand, no jump but "loop $"), else None."""
    if i + 3 > len(b):
        return None
    op = b[i]
    if op in (0x27, 0x2F, 0x37, 0x3F, 0x90, 0x98, 0x99, 0xF8, 0xF9, 0xFC, 0xFD, 0x9C, 0x9D) \
            or 0x40 <= op <= 0x5F:
        return 1
    if op in _ALU8 or 0xB0 <= op <= 0xB7:
        return 2
    if op in _ALU16 or 0xB8 <= op <= 0xBF:
        return 3
    if op in _REGREG and b[i + 1] >= 0xC0:
        return 2
    if op in (0xC0, 0xC1) and b[i + 1] >= 0xC0:
        return 3
    if op == 0xE2 and b[i + 1] == 0xFE:          # loop $
        return 2
    if op == 0xE6 and b[i + 1] == 0xB5:          # out 0B5h, al
        return 2
    if op == 0xE4 and b[i + 1] == 0xB5:          # in al, 0B5h
        return 2
    if op in (0xEC, 0xEE):                       # in al, dx / out dx, al
        return 1
    if op == 0xF3 and b[i + 1] == 0xE4 and b[i + 2] == 0xB5:   # rep in al, 0B5h
        return 3
    if 0x70 <= op <= 0x7F or op in (0xEB, 0xE2):  # short jumps: only inside a block
        return 2
    return None


def _jumps_inside(b, s, e):
    """Every short jump of [s, e) lands inside [s, e)."""
    p = s
    while p < e:
        n = ilen(b, p)
        if n is None:
            return False
        op = b[p]
        if 0x70 <= op <= 0x7F or op in (0xEB, 0xE2):
            t = p + 2 + (b[p + 1] - 256 if b[p + 1] > 127 else b[p + 1])
            if not s <= t < e:
                return False
        p += n
    return True


def _jump_targets_inside(bank, s, e):
    """Does any jump of the bank (byte scan, conservative) land in (s, e)?"""
    for p in range(max(0, s - 130), min(len(bank) - 2, e + 130)):
        op = bank[p]
        if 0x70 <= op <= 0x7F or op in (0xEB, 0xE0, 0xE1, 0xE2, 0xE3):
            if s <= p < e:
                continue
            t = p + 2 + (bank[p + 1] - 256 if bank[p + 1] > 127 else bank[p + 1])
            if s < t < e:
                return True
    for m in re.finditer(rb'[\xE8\xE9]', bank):
        p = m.start()
        if p + 3 > len(bank) or s <= p < e:
            continue
        t = (p + 3 + int.from_bytes(bank[p + 1:p + 3], 'little')) & 0xFFFF
        if s < t < e:
            return True
    return False


def trampoline(data, caves=False):
    geo = Geometry(data)
    work = bytearray(data)
    space = Space(work, geo, caves)
    hook = None
    notes = []
    banks = geo.linear_banks() + [b for b in range(geo.first - 1, -1, -1)]
    for b in banks:
        base = b << 16
        bank = bytes(data[base:base + 0x10000])
        i = bank.find(b'\xB0\x40\xE6\xB5')
        while 0 <= i < 0xFF00:
            # decode forward from the "mov al, 40h"
            p, ins, read = i, [], None
            while p < i + 48:
                n = ilen(bank, p)
                if n is None:
                    break
                ins.append((p, n))
                if bank[p] == 0xE4:
                    read = len(ins) - 1
                    break
                p += n
            if read is not None:
                # add simple instructions after the read
                p = ins[-1][0] + 2
                while True:
                    n = ilen(bank, p)
                    if n is None or bank[p] in (0xE4, 0xE6) or len(ins) - read > 4:
                        break
                    ins.append((p, n))
                    p += n
                # smallest block around the read with >= 5 bytes, starting
                # after "mov al,40h / out" when possible
                best = None
                for a in range(read, -1, -1):
                    for z in range(read, len(ins)):
                        s, e = ins[a][0], ins[z][0] + ins[z][1]
                        if e - s >= 5 and (best is None or e - s < best[1] - best[0]):
                            best = (s, e)
                if best and not _jumps_inside(bank, best[0], best[1]):
                    best = None
                if best and not _jump_targets_inside(bank, best[0], best[1]):
                    s, e = best
                    if hook is None:
                        hook = space.alloc(len(HOOK), geo.linear_banks())
                        if hook is None:
                            return [], 'no free space for the hook'
                        work[hook:hook + len(HOOK)] = HOOK
                    code = bank[s:e]
                    k = code.find(b'\xE4\xB5', ins[read][0] - s)
                    tr = code[:k] + far_call(geo, hook + HOOK_PLAIN) + code[k + 2:] + b'\xCB'
                    t = space.alloc(len(tr), geo.linear_banks(), margin=8)
                    if t is None:
                        return [], 'no free space for the routine'
                    work[t:t + len(tr)] = tr
                    work[base + s:base + e] = far_call(geo, t) + b'\x90' * (e - s - 5)
                    notes.append('%06X' % (base + s))
            i = bank.find(b'\xB0\x40\xE6\xB5', i + 1)
            if len(notes) >= 4:
                break
    if not notes:
        return [], 'no button read with movable code around it'
    return [Candidate('trampoline', bytes(work), 'reads at ' + ', '.join(notes),
                      risk='' if space.kind == 'FF' else 'code in a 00h area')], ''


# ------------------------------------------------------------------ port

def _fill(a):
    return bool(a) and (set(a) <= {0xFF} or set(a) <= {0x00})


def _size_from_key(key):
    return int(key[-4:], 16) << 16


def port(data, db_dirs, caves=False, everything=False):
    """Patches of other ROMs with the same publisher and game id (or all of
    them with everything=True) whose code bytes are found here."""
    key = rom_key(data)
    geo = Geometry(data)
    out = []
    seen = set()
    for d in db_dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.upper().endswith('.Y1P') or fn[:-4].upper() == key:
                continue
            if not everything and fn[:4].upper() != key[:4]:
                continue
            src_geo = Geometry(b'\0' * _size_from_key(fn[:-4]))
            for name, crc, recs in read_y1p(os.path.join(d, fn)):
                for got in _port_variant(data, geo, src_geo, recs, caves):
                    if got[0] not in seen:
                        seen.add(got[0])
                        out.append(Candidate('port', got[0], 'from %s [%s] %s' % (fn, name, got[1])))
    return out


def _port_variant(data, geo, src_geo, recs, caves):
    code = [r for r in recs if not _fill(r[1])]
    fill = [r for r in recs if _fill(r[1])]
    found = []
    if not code:
        return found
    anchor = max(code, key=lambda r: len(r[1]))
    if len(anchor[1]) < 5:
        return found
    occ, start = [], 0
    while len(occ) < 16:
        i = data.find(anchor[1], start)
        if i < 0:
            break
        occ.append(i)
        start = i + 1
    # same place in the bank first (a moved bank), then the others
    occ.sort(key=lambda o: ((o - anchor[0]) & 0xFFFF) != 0)
    for o in occ:
        d = o - anchor[0]
        if not all(data[r[0] + d:r[0] + d + len(r[1])] == r[1] for r in code):
            continue
        work = bytearray(data)
        lo = min(r[0] for r in fill) if fill else 0
        hi = max(r[0] + len(r[2]) for r in fill) if fill else 0
        if not fill or all(data[r[0] + d:r[0] + d + len(r[1])] == r[1] for r in fill):
            nd = d
            note = 'shift %+X' % d
        else:
            space = Space(work, geo, caves)
            new = space.alloc(hi - lo, geo.linear_banks())
            if new is None:
                continue
            nd = new - lo
            note = 'shift %+X, new code moved %+X' % (d, nd)

        def fix(b, at):
            """far targets into the old new-code block -> moved; near
            calls into it are fine only if the block moved with them."""
            b = bytearray(b)
            i = 0
            while i + 5 <= len(b):
                if b[i] in (0x9A, 0xEA):
                    off = b[i + 1] | b[i + 2] << 8
                    seg = b[i + 3] | b[i + 4] << 8
                    t = src_geo.file_of(seg, off)
                    if t is not None and lo <= t < hi:
                        nt = t + nd
                        if not geo.linear(nt >> 16):
                            return None
                        no, ns = geo.far(nt)
                        b[i + 1:i + 5] = _w(no) + _w(ns)
                        i += 5
                        continue
                if b[i] in (0xE8, 0xE9) and nd != d:
                    t = ((at + i) & ~0xFFFF) | ((at + i + 3 + (b[i + 1] | b[i + 2] << 8)) & 0xFFFF)
                    if lo + d <= t < hi + d:
                        return None
                i += 1
            return bytes(b)
        ok = True
        for r in code:
            nb = fix(r[2], r[0] + d)
            if nb is None:
                ok = False
                break
            work[r[0] + d:r[0] + d + len(nb)] = nb
        for r in fill:
            nb = fix(r[2], r[0] + nd)
            if nb is None or not ok:
                ok = False
                break
            if any(x not in (0x00, 0xFF) for x in data[r[0] + nd:r[0] + nd + len(nb)]):
                ok = False
                break
            work[r[0] + nd:r[0] + nd + len(nb)] = nb
        if ok:
            found.append((bytes(work), note))
    return found


# ------------------------------------------------------------------ inportb

_INPORTB = re.compile(rb'\x55(\x8B\xEC|\x89\xE5)((?:\x56\x57)?)\x8B\x56\x06\xEC'
                      rb'((?:\x30\xE4|\x32\xE4)?)((?:\x5F\x5E)?)\x5D\xCB')


def _wait():
    return bytes.fromhex('B91800E2FE')


def inportb(data, caves=False):
    geo = Geometry(data)
    work = bytearray(data)
    space = Space(work, geo, caves)
    out_notes, tests = [], []
    for m in _INPORTB.finditer(data):
        f = m.start()
        mov_bp, push, clr, pop = m.group(1), m.group(2), m.group(3), m.group(4)
        head = b'\x55' + mov_bp + push + b'\x8B\x56\x06\xEC'
        tail = clr + pop + b'\x5D\xCB'
        y = b'\x51\xB0\x10\xE6\xB5' + _wait() + b'\xE4\xB5\x88\xC4\xB0\x40\xE6\xB5' + _wait()
        after_y = b'\xF6\xC4\x01' + _jcc(0x75, 3) + b'\x59\x58\x9D'
        # layout: head, cmp dx,B5, jne done, pushf, test al,2, jz popf,
        # push ax, and al,70h, cmp al,40h, jne popax, y-read, test ah,1,
        # jnz reset, pop cx, popax: pop ax, popf: popf, done: tail, reset
        body_end = b'\x59'                        # pop cx (no reset)
        popax = b'\x58'
        popf = b'\x9D'
        reset_dist = len(body_end) + len(popax) + len(popf) + len(tail)
        part3 = y + b'\xF6\xC4\x01' + _jcc(0x75, reset_dist) + body_end
        part2 = b'\x50\x24\x70\x3C\x40' + _jcc(0x75, len(part3)) + part3
        part1 = b'\x9C\xA8\x02' + _jcc(0x74, len(part2) + len(popax)) + part2
        cmp = b'\x81\xFA\xB5\x00' + _jcc(0x75, len(part1) + len(popax) + len(popf))
        wrapper = head + cmp + part1 + popax + popf + tail + RESET
        w = space.alloc(len(wrapper), geo.linear_banks())
        if w is None:
            return [], 'no free space for the wrapper'
        work[w:w + len(wrapper)] = wrapper
        n = m.end() - f
        work[f:f + n] = far_jump(geo, w) + b'\x90' * (n - 5)
        out_notes.append('%06X' % f)
        seg = geo.seg(f >> 16)
        for port_, sel, reset in ((0xB5, 0x40, True), (0xB5, 0x10, False), (0xB5, 0x20, False),
                                  (0xA0, 0x40, False)):
            tests.append(dict(name='inportb(%02X) group %02X' % (port_, sel), start=f, sel=sel,
                              stack=(0x0100, 0x0000, port_), expect_reset=reset, cs=seg))
    if not out_notes:
        return [], 'no inportb() function'
    return [Candidate('inportb', bytes(work), 'function at ' + ', '.join(out_notes), tests,
                      risk='' if space.kind == 'FF' else 'code in a 00h area')], ''


# ------------------------------------------------------------------ nopsled

def _y_store(bank, before):
    """(memory address) where the Y group (10h) was stored before `before`,
    from "mov al,10h / out 0B5h,al / ... in al,0B5h / and al|ax,0Fh / mov|add
    [addr], ax|al" within 0x200 bytes; also the select's offset."""
    lo = max(0, before - 0x200)
    i = bank.rfind(b'\xB0\x10\xE6\xB5', lo, before)
    if i < 0:
        return None
    j = bank.find(b'\xE4\xB5', i, i + 0x60)
    if j < 0:
        return None
    k = j + 2
    if bank[k:k + 2] == b'\x24\x0F':
        k += 2
    elif bank[k:k + 3] == b'\x25\x0F\x00':
        k += 3
    else:
        return None
    if bank[k] in (0xA3, 0xA2):                        # mov [addr], ax/al
        return bank[k + 1] | bank[k + 2] << 8, i
    if bank[k] in (0x89, 0x88, 0x01) and bank[k + 1] == 0x06:   # mov/add [addr], ax
        return bank[k + 2] | bank[k + 3] << 8, i
    return None


def _self_check():
    """In the room of a delay: wait, read, START? then read Y1 (group back
    to 40h, AL = the buttons as the game reads them), reset on both."""
    d = b'\x51\xB9\x0C\x00\xE2\xFE\x59'
    w = b'\x51\xB9\x18\x00\xE2\xFE\x59'
    ychk = b'\x50\xB0\x10\xE6\xB5' + w + b'\xE4\xB5\xA8\x01\xB0\x40\xE6\xB5' + w + b'\x58'
    after = ychk + _jcc(0x74, len(RESET)) + RESET
    return d + b'\xE4\xB5\xA8\x02' + _jcc(0x74, len(after)) + after


def nopsled(data, min_nops=24):
    geo = Geometry(data)
    work = bytearray(data)
    tests, notes = [], []
    for m in re.finditer(rb'\xB0\x40\xE6\xB5(\x90{%d,})\xE4\xB5' % min_nops, data):
        s = m.start() + 4
        e = m.end()                                 # after the IN
        room = e - s
        base = s & ~0xFFFF
        bank = bytes(data[base:base + 0x10000])
        ys = _y_store(bank, s - base)
        if ys:
            addr, ysel = ys
            ytest = b'\xF6\x06' + _w(addr) + b'\x01'
            tail = ytest + _jcc(0x74, len(RESET)) + RESET
            code = b'\x51\xB9\x0C\x00\xE2\xFE\x59\xE4\xB5\xA8\x02' + _jcc(0x74, len(tail)) + tail
            start = base + ysel
            sel = 0x10
        else:
            code = _self_check()
            start, sel = m.start(), 0x40
        if len(code) > room:
            continue
        work[s:e] = b'\x90' * (room - len(code)) + code
        notes.append('%06X' % s)
        tests.append(dict(name='nopsled', start=start, stop=e, sel=sel, flagext=True))
    if not notes:
        return [], 'no delay of NOPs long enough'
    return [Candidate('nopsled', bytes(work), 'NOP delay at ' + ', '.join(notes), tests)], ''


# ------------------------------------------------------------------ cblock

def _units(data, p):
    """Bit tests "test word [bp+d], t / je +9 / mov ax,[bp+m] / or ax, v /
    mov [bp+m], ax" from p: (d, m, [(t, v)], end)."""
    out, d0, m0 = [], None, None
    while p + 16 <= len(data):
        b = data[p:p + 16]
        if not (b[0] == 0xF7 and b[1] == 0x46 and b[5:7] == b'\x74\x09' and b[7:9] == b'\x8B\x46'
                and b[10] == 0x0D and b[13:15] == b'\x89\x46' and b[15] == b[9]):
            break
        d, m = b[2], b[9]
        if d0 is None:
            d0, m0 = d, m
        elif (d, m) != (d0, m0):
            break
        out.append((b[3] | b[4] << 8, b[11] | b[12] << 8))
        p += 16
    return d0, m0, out, p


def cblock(data):
    work = bytearray(data)
    tests, notes = [], []
    # "mov dx,0B5h / mov al,g / out dx,al / mov dx,0B5h / in al,dx / mov ah,0 /
    #  and ax,0Fh / mov [bp+d],ax" then the bit tests
    rd = re.compile(rb'\xBA\xB5\x00\xB0([\x10\x20\x40])\xEE\xBA\xB5\x00\xEC\xB4\x00\x25\x0F\x00\x89\x46(.)', re.S)
    blocks = []
    for m in rd.finditer(data):
        d, mm, units, end = _units(data, m.end())
        if units and d == m.group(2)[0]:
            blocks.append(dict(group=m.group(1)[0], read=m.start(), start=m.end(), end=end, m=mm,
                               units=units))
    for i, blk in enumerate(blocks):
        if blk['group'] not in (0x10, 0x40):
            continue
        other = 0x40 if blk['group'] == 0x10 else 0x10
        prev = [x for x in blocks[:i] if x['group'] == other and x['m'] == blk['m']
                and 0 < blk['read'] - x['read'] < 0x200]
        if not prev:
            continue
        pv = prev[-1]
        ybl, bbl = (blk, pv) if blk['group'] == 0x10 else (pv, blk)
        y1 = [v for t, v in ybl['units'] if t == 1]
        st = [v for t, v in bbl['units'] if t == 2]
        if not y1 or not st:
            continue
        mask = 0
        k = None
        for t, v in blk['units']:
            mask |= t
            sh = v.bit_length() - t.bit_length()
            if sh < 0 or (t << sh) != v or (k is not None and k != sh):
                k = -1
                break
            k = sh
        if k is None or k < 0:
            continue
        both = y1[0] | st[0]
        new = b''
        if mask != 0x0F:
            new += b'\x25' + _w(mask)
        if k:
            new += b'\xC1\xE0' + bytes([k])
        new += b'\x09\x46' + bytes([blk['m']])
        new += b'\x8B\x46' + bytes([blk['m']]) + b'\x25' + _w(both) + b'\x3D' + _w(both)
        room = blk['end'] - blk['start']
        pad = room - len(new) - 2 - len(RESET)
        if pad < 0:
            continue
        new += _jcc(0x75, len(RESET) + pad) + RESET + b'\x90' * pad
        # where AX is loaded again after the block (the test stops there)
        # where AX is loaded again after the block (the test stops there):
        # through "mov dx,0B5h / mov al,n / out dx,al", up to "mov ax,[bp+m]"
        # or "mov dx,0B5h / in al,dx / mov ah,0"
        e, stop = blk['end'], None
        while e < blk['end'] + 24:
            if data[e:e + 3] == b'\x8B\x46' + bytes([blk['m']]):
                stop = e + 3
                break
            if data[e:e + 6] == b'\xBA\xB5\x00\xEC\xB4\x00':
                stop = e + 6
                break
            if data[e:e + 3] == b'\xBA\xB5\x00':
                e += 3
            elif data[e] == 0xB0:
                e += 2
            elif data[e] == 0xEE:
                e += 1
            else:
                break
        if stop is None:
            continue
        work[blk['start']:blk['end']] = new
        notes.append('%06X' % blk['start'])
        tests.append(dict(name='cblock', start=pv['read'], stop=stop, sel=0x40))
    if not notes:
        return [], 'no C bit-copy block'
    return [Candidate('cblock', bytes(work), 'blocks at ' + ', '.join(notes), tests)], ''


# ------------------------------------------------------------------ anyread

def _is_read(b, p):
    if b[p] == 0xE4 and b[p + 1] == 0xB5:
        return 2
    if b[p] == 0xF3 and b[p + 1] == 0xE4 and b[p + 2] == 0xB5:
        return 3
    if b[p] == 0xEC:
        # in al, dx: only with DX set to 0B5h just before, or an
        # "mov al, group / out dx, al" right before
        w = b[max(0, p - 24):p]
        if b'\xBA\xB5\x00' in w or re.search(rb'\xB0[\x10\x20\x40]\xEE', w):
            return 1
    return 0


_BEFORE_READ = (rb'\xB0[\x10\x20\x40]\xE6\xB5\x90*\Z',
                rb'\xB0[\x10\x20\x40]\xEE\x27?\x90*\Z',
                rb'\xB9..\xE2\xFE\x90*\Z',
                rb'\x90{2,}\Z',
                rb'\x27\Z')


def anyread(data, caves=False, max_reads=8):
    """Before any keypad read (any group, any shape) with movable code
    around it: save the selected group, call the hook (selects 40h, START+Y1
    -> reset), select the group again, wait, then run the game's own
    instructions (moved to free space)."""
    geo = Geometry(data)
    work = bytearray(data)
    space = Space(work, geo, caves)
    hook = None
    notes = []
    banks = geo.linear_banks() + [b for b in range(geo.first - 1, -1, -1)]
    for b in banks:
        base = b << 16
        bank = bytes(data[base:base + 0x10000])
        for m in re.finditer(rb'\xE4\xB5|\xEC', bank):
            p = m.start()
            if p >= 0xFF00 or len(notes) >= max_reads:
                continue
            if p > 0 and bank[p - 1] == 0xF3 and bank[p] == 0xE4:
                p -= 1                                  # rep in al, 0B5h
            n = _is_read(bank, p)
            if not n:
                continue
            # movable block [s, e) around the read, >= 5 bytes: backwards
            # only over shapes that cannot be the tail of another
            # instruction, forwards over simple instructions
            s0 = p
            w = bank[max(0, p - 12):p]
            for pat in _BEFORE_READ:
                m2 = re.search(pat, w)
                if m2:
                    s0 = min(s0, p - (len(w) - m2.start()))
            best = None
            for s in sorted({s0, p}):
                q = p + n
                while q - s < 5:
                    k = ilen(bank, q)
                    if k is None or bank[q] in (0xE4, 0xE6, 0xEC, 0xEE):
                        break
                    q += k
                if q - s >= 5 and _jumps_inside(bank, s, q):
                    best = (s, q)
                    break
            if not best or _jump_targets_inside(bank, best[0], best[1]):
                continue
            s, e = best
            if hook is None:
                hook = space.alloc(len(HOOK), geo.linear_banks())
                if hook is None:
                    return [], 'no free space for the hook'
                work[hook:hook + len(HOOK)] = HOOK
            pre = (b'\x50\xE4\xB5\x24\x70\x50' + far_call(geo, hook) +
                   b'\x58\xE6\xB5\x51\xB9\x18\x00\xE2\xFE\x59\x58')
            tr = pre + bank[s:e] + b'\xCB'
            t = space.alloc(len(tr), geo.linear_banks(), margin=8)
            if t is None:
                break
            work[t:t + len(tr)] = tr
            work[base + s:base + e] = far_call(geo, t) + b'\x90' * (e - s - 5)
            notes.append('%06X' % (base + p))
    if not notes:
        return [], 'no keypad read with movable code around it'
    return [Candidate('anyread', bytes(work), 'reads at ' + ', '.join(notes),
                      risk='' if space.kind == 'FF' else 'code in a 00h area')], ''


# ------------------------------------------------------------------ vblank

def vblank(data, caves=False):
    if not keypad_free(data):
        return [], 'the game reads the keys'
    geo = Geometry(data)
    work = bytearray(data)
    space = Space(work, geo, caves)
    notes, tests = [], []
    for m in re.finditer(rb'\xB0\x40\xE6\xB6([\x07\x1F\x58-\x5F\x61]{1,6})\xCF', data):
        pops = m.group(1)
        s = m.start()
        w = b'\x51\xB9\x18\x00\xE2\xFE\x59'
        chk = (b'\x50\xB0\x40\xE6\xB5' + w + b'\xE4\xB5\xA8\x02')
        y = b'\xB0\x10\xE6\xB5' + w + b'\xE4\xB5\xA8\x01'
        done = b'\x58\x83\xC4\x04' + pops + b'\xCF'
        part = y + _jcc(0x74, len(RESET)) + RESET
        x = b'\xB0\x40\xE6\xB6' + chk + _jcc(0x74, len(part)) + part + done
        a = space.alloc(len(x), geo.linear_banks())
        if a is None:
            return [], 'no free space'
        work[a:a + len(x)] = x
        work[s:s + 5] = far_call(geo, a)
        notes.append('%06X' % s)
        nwords = sum(8 if p == 0x61 else 1 for p in pops)
        stack = tuple(0x1111 * (i + 1) & 0xFFFF for i in range(nwords)) + (0x0100, 0x0000, 0x0202)
        if 0x61 in pops:                           # popa skips the SP slot
            idx = sum(8 if p == 0x61 else 1 for p in pops[:pops.index(0x61)]) + 3
            stack = stack[:idx] + (0x0EFE,) + stack[idx + 1:]
        tests.append(dict(name='vblank', start=s, sel=0x00, stack=stack, ignore_sel=True))
    if not notes:
        return [], 'no VBlank handler found'
    return [Candidate('vblank', bytes(work), 'VBlank handler at ' + ', '.join(notes), tests)], ''


ORDER = ('database', 'classic', 'classic+', 'trampoline', 'port', 'inportb', 'nopsled',
         'cblock', 'anyread', 'vblank')
