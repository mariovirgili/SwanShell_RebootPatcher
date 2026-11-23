# SPDX-License-Identifier: GPL-3.0-or-later
# SwanShellPatcher - Copyright (C) 2026 Mario Virgili
"""Checks a patch by running the game's code, original and patched, on the
small V30MZ interpreter in cpu.py.

For every patched spot the routine runs from its start (the group select
before the button read) to the end of the patched bytes, with each of
these keys held: none, A, B, START, Y1, START+A, X1, Y2. The patched run
must end in exactly the same state as the original one (registers, RAM,
the keypad group selected, the other ports written). With START + Y1 held
the patched run must reach the soft reset (the write DDh to port E2h, or
FFFF:0000).

The patch is fine for the CPU; it is not a test of the whole game (timing,
graphics in a code cave...): play it on the console as well.
"""
from .cpu import CPU, CPUError, decode, AX, CX, DX, BX, SP, BP, SI, DI, ES, CS, SS, DS
from .rom import Geometry


COMBOS = (('none', {}), ('A', {'B': 4}), ('B', {'B': 8}), ('START', {'B': 2}), ('Y1', {'Y': 1}),
          ('START+A', {'B': 6}), ('X1', {'X': 1}), ('Y2', {'Y': 2}))
RESET_KEYS = {'B': 2, 'Y': 1}
NAMES = 'AX BX CX DX SI DI BP SP FLAGS ES DS SS'.split()
SENTINEL = 0x0100                     # 0000:0100, return address of test calls
_REG = {'AX': AX, 'BX': BX, 'CX': CX, 'DX': DX, 'SI': SI, 'DI': DI, 'BP': BP, 'SP': SP}
_SEG = {'ES': ES, 'DS': DS, 'SS': SS}


class Run:
    """One run: the end state of the CPU."""

    def __init__(self, data, start, keys, sel0, stop=None, stack=(), regs=None, ram=None,
                 cs=None, maxins=500000):
        geo = Geometry(data)
        mem = bytearray(0x100000)
        for n in range(4, 16):
            b = geo.first + n if geo.banks >= 16 else n % geo.banks
            if geo.banks >= 16 and b >= geo.banks:
                continue
            chunk = data[b << 16:(b + 1) << 16]
            mem[n << 16:(n << 16) + len(chunk)] = chunk
        bank = start >> 16
        seg = geo.seg(bank) if cs is None else cs
        if seg == 0x2000:
            chunk = data[bank << 16:(bank + 1) << 16]
            mem[0x20000:0x20000 + len(chunk)] = chunk
            mem[0x30000:0x30000 + len(chunk)] = chunk
        self.sel, self.reset, self.err, self.outs, self.ended = sel0, False, None, [], False
        cpu = CPU(mem)

        def pin(port, size):
            if port == 0xB5:
                v = 0
                if self.sel & 0x10:
                    v |= keys.get('Y', 0)
                if self.sel & 0x20:
                    v |= keys.get('X', 0)
                if self.sel & 0x40:
                    v |= keys.get('B', 0)
                return (self.sel | v) & 0xFF
            return 0

        def pout(port, size, val):
            if port == 0xB5:
                self.sel = val & 0x70
            else:
                self.outs.append((port, val & 0xFFFF))
            if port == 0xE2 and (val & 0xFF) == 0xDD:
                self.reset = True
                cpu.stopped = True
        cpu.port_in, cpu.port_out = pin, pout
        stop_lin = None if stop is None else (seg << 4) + (stop & 0xFFFF)

        def before(lin):
            if lin == SENTINEL or lin == stop_lin:
                self.ended = True
                return True
            if lin == 0xFFFF0:
                self.reset = True
                return True
            return False
        init = dict(zip(NAMES, (0x1234, 0x0222, 0x5678, 0x00B5, 0x0444, 0x0566, 0x0E00, 0x0F00)))
        init.update(regs or {})
        for name, i in _REG.items():
            cpu.r[i] = init[name] & 0xFFFF
        for name, i in _SEG.items():
            cpu.s[i] = init.get(name, 0) & 0xFFFF
        if 'FLAGS' in init and regs and 'FLAGS' in regs:
            cpu.flags = init['FLAGS']
        for w in reversed(stack):
            cpu.r[SP] = (cpu.r[SP] - 2) & 0xFFFF
            mem[cpu.r[SP]] = w & 0xFF
            mem[cpu.r[SP] + 1] = (w >> 8) & 0xFF
        cpu.s[CS] = seg
        cpu.ip = start & 0xFFFF
        for a, v in (ram or {}).items():
            mem[a] = v
        try:
            done = cpu.run(maxins, before)
            if not done:
                self.err = 'too many instructions'
            elif cpu.halted:
                self.err = 'hlt'
        except CPUError as e:
            self.err = str(e)
        regs_out = {n: cpu.r[i] for n, i in _REG.items()}
        regs_out.update({n: cpu.s[i] for n, i in _SEG.items()})
        regs_out['FLAGS'] = cpu.flags
        self.regs = regs_out
        # RAM without the test stack (0E00h-0FFFh): a far call leaves its
        # return address below SP, which is not a difference for the game
        self.ram = bytes(mem[0:0xE00]) + bytes(mem[0x1000:0x10000])


def same(a, b, ignore=('FLAGS',), sel=True):
    if not (a.ended and b.ended) or b.reset:
        return False
    regs = all(a.regs[n] == b.regs[n] for n in NAMES if n not in ignore)
    return regs and a.ram == b.ram and (a.sel == b.sel or not sel) and a.outs == b.outs


def _flag_ext(rom, e):
    """One more instruction when it rewrites the flags (and al, 0Fh...)."""
    d = decode(rom, e)
    if d and d[1] == 'flags':
        e += d[0]
    return e


def run_test(orig, new, test):
    """test: dict(start, stop, sel, stack, regs, ram, cs, ignore, name).
    Returns (ok, text)."""
    if test.get('flagext') and test.get('stop') is not None:
        test = dict(test, stop=_flag_ext(orig, test['stop']))
    kw = dict(stop=test.get('stop'), stack=test.get('stack', ()), regs=test.get('regs'),
              ram=test.get('ram'), cs=test.get('cs'))
    ignore = test.get('ignore', ('FLAGS',))
    res, ok = [], True
    for name, keys in COMBOS:
        a = Run(orig, test['start'], keys, test.get('sel', 0x40), **kw)
        if a.err or not a.ended:
            res.append(name + ':?')
            ok = False
            continue
        b = Run(new, test['start'], keys, test.get('sel', 0x40), **kw)
        s = same(a, b, ignore, not test.get('ignore_sel'))
        ok &= s
        res.append(name + (':=' if s else ':DIFF'))
    keys = dict(RESET_KEYS)
    r = Run(new, test['start'], keys, test.get('sel', 0x40), **kw)
    if test.get('expect_reset', True):
        res.append('START+Y1:' + ('reset' if r.reset else 'NO RESET'))
        ok &= r.reset
    else:
        a = Run(orig, test['start'], keys, test.get('sel', 0x40), **kw)
        s = same(a, r, ignore, not test.get('ignore_sel'))
        res.append('START+Y1:' + ('= (no reset, as it should)' if s else 'DIFF'))
        ok &= s
    return ok, '%s %06X: %s' % (test.get('name', 'site'), test['start'], ' '.join(res))


# --- tests for patches that only change a button read ---------------------

def find_start(rom, site):
    """The nearest "mov al, imm / out 0B5h" before the patched bytes (within
    96 bytes, not across a return or jump): (start, group selected)."""
    lo = max(site - 96, site & ~0xFFFF)
    for i in range(site, lo - 1, -1):
        if rom[i] == 0xB0 and rom[i + 2] == 0xE6 and rom[i + 3] == 0xB5:
            start, sel = i, rom[i + 1] & 0x70
            break
        if rom[i] == 0xB8 and rom[i + 3] in (0xE7, 0xE6) and rom[i + 4] == 0xB5:
            start, sel = i, rom[i + 1] & 0x70
            break
    else:
        return site, 0x40
    p = start
    while p < site:
        d = decode(rom, p)
        if d is None:
            break
        if d[1] in ('ret', 'jmp'):
            return site, 0x40
        p += d[0]
    return start, sel


def is_fill(a):
    return bool(a) and (set(a) <= {0xFF} or set(a) <= {0x00})


def site_tests(rom, records):
    """Tests for every group of patched code bytes (records over FFh/00h
    fill are the new code: they are reached through the patched sites)."""
    sites = sorted((o, len(b)) for o, a, b in records if not is_fill(a))
    groups = []
    for o, n in sites:
        if groups and o - groups[-1][1] < 0x100 and (o >> 16) == (groups[-1][0] >> 16):
            groups[-1][1] = max(groups[-1][1], o + n)
        else:
            groups.append([o, o + n])
    tests = []
    for s, e in groups:
        # a record may start after unchanged bytes of an instruction
        # (9A xx xx xx xx -> 9A yy yy yy yy): start at the instruction
        for back, op in ((1, (0x9A, 0xEA, 0xE8, 0xE9)),):
            if s - back >= 0 and rom[s - back] in op:
                s -= back
        start, sel = find_start(rom, s)
        tests.append(dict(name='site', start=start, stop=_flag_ext(rom, e), sel=sel))
    return tests


def bx_values(records):
    """Values some trampolines compare BX with (cmp bx, imm) before calling
    the hook: the test is run again with them."""
    out = []
    for o, a, b in records:
        for i in range(len(b) - 3):
            if b[i] == 0x83 and b[i + 1] == 0xFB:
                out.append(b[i + 2])
            if b[i] == 0x81 and b[i + 1] == 0xFB and i + 3 < len(b):
                out.append(b[i + 2] | b[i + 3] << 8)
    return out


def verify(orig, new, records, tests=None):
    """(verdict, lines). verdict: 'OK', 'FAIL' or 'UNVERIFIED' (no test
    could run)."""
    tests = tests if tests is not None else site_tests(orig, records)
    if not tests:
        return 'UNVERIFIED', ['nothing to run']
    lines, ok, unknown = [], True, False
    for t in tests:
        good, text = run_test(orig, new, t)
        if not good:
            for bx in bx_values(records):
                t2 = dict(t, regs=dict(t.get('regs') or {}, BX=bx))
                g2, text2 = run_test(orig, new, t2)
                if g2:
                    good, text = True, text2 + ' (BX=%04X)' % bx
                    break
        if not good and all(x.endswith(':?') for x in text.split(': ', 1)[1].split()[:len(COMBOS)]):
            unknown = True                     # the original code itself could not run
        elif not good:
            ok = False
        lines.append(text)
    if ok and unknown:
        return 'UNVERIFIED', lines + ['the original code could not be run from this point']
    return ('OK' if ok else 'FAIL'), lines
