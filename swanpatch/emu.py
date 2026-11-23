"""Checks a patch by running the game's code, original and patched, on an
emulated V30MZ (x86 16-bit) CPU with the Unicorn engine.

    pip install unicorn capstone

For every patched spot the routine runs from its start (the group select
before the button read) to the end of the patched bytes, with each of
these keys held: none, A, START, Y1, START+A, X1, B. The patched run must
end in exactly the same state as the original one (registers, RAM, the
keypad group selected). With START + Y1 held the patched run must reach the
soft reset (the write DDh to port E2h, or FFFF:0000).

The patch is fine for the CPU; it is not a test of the whole game (timing,
graphics in a code cave...): play it on the console as well.
"""
try:
    from unicorn import Uc, UcError, UC_ARCH_X86, UC_MODE_16, UC_HOOK_CODE, UC_HOOK_INSN
    from unicorn.x86_const import (UC_X86_REG_AX, UC_X86_REG_BX, UC_X86_REG_CX, UC_X86_REG_DX,
                                   UC_X86_REG_SI, UC_X86_REG_DI, UC_X86_REG_BP, UC_X86_REG_SP,
                                   UC_X86_REG_FLAGS, UC_X86_REG_ES, UC_X86_REG_DS, UC_X86_REG_SS,
                                   UC_X86_REG_CS, UC_X86_REG_IP, UC_X86_INS_IN, UC_X86_INS_OUT)
    AVAILABLE = True
except ImportError:                                  # pragma: no cover
    AVAILABLE = False

try:
    from capstone import Cs, CS_ARCH_X86, CS_MODE_16
    _MD = Cs(CS_ARCH_X86, CS_MODE_16)
except ImportError:                                  # pragma: no cover
    _MD = None

from .rom import Geometry

COMBOS = (('none', {}), ('A', {'B': 4}), ('B', {'B': 8}), ('START', {'B': 2}), ('Y1', {'Y': 1}),
          ('START+A', {'B': 6}), ('X1', {'X': 1}), ('Y2', {'Y': 2}))
RESET_KEYS = {'B': 2, 'Y': 1}
NAMES = 'AX BX CX DX SI DI BP SP FLAGS ES DS SS'.split()
SENTINEL = 0x0100                     # 0000:0100, return address of test calls


def _regs():
    return [UC_X86_REG_AX, UC_X86_REG_BX, UC_X86_REG_CX, UC_X86_REG_DX, UC_X86_REG_SI,
            UC_X86_REG_DI, UC_X86_REG_BP, UC_X86_REG_SP, UC_X86_REG_FLAGS, UC_X86_REG_ES,
            UC_X86_REG_DS, UC_X86_REG_SS]


class Run:
    """One run: the end state of the CPU."""

    def __init__(self, data, start, keys, sel0, stop=None, stack=(), regs=None, ram=None,
                 cs=None, maxins=500000):
        geo = Geometry(data)
        mu = Uc(UC_ARCH_X86, UC_MODE_16)
        mu.mem_map(0, 0x110000)
        for n in range(4, 16):
            b = geo.first + n if geo.banks >= 16 else n % geo.banks
            if geo.banks >= 16 and b >= geo.banks:
                continue
            mu.mem_write(n << 16, data[b << 16:(b + 1) << 16])
        bank = start >> 16
        seg = geo.seg(bank) if cs is None else cs
        if seg == 0x2000:
            mu.mem_write(0x20000, data[bank << 16:(bank + 1) << 16])
            mu.mem_write(0x30000, data[bank << 16:(bank + 1) << 16])
        self.sel, self.reset, self.err, self.outs = sel0, False, None, []

        def hin(uc, port, size, ud):
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

        def hout(uc, port, size, val, ud):
            if port == 0xB5:
                self.sel = val & 0x70
            else:
                self.outs.append((port, val & 0xFFFF))
            if port == 0xE2 and (val & 0xFF) == 0xDD:
                self.reset = True
                uc.emu_stop()

        mu.hook_add(UC_HOOK_INSN, hin, None, 1, 0, UC_X86_INS_IN)
        mu.hook_add(UC_HOOK_INSN, hout, None, 1, 0, UC_X86_INS_OUT)
        stop_lin = None if stop is None else (seg << 4) + (stop & 0xFFFF)
        self.ended = False

        def code(uc, addr, size, ud):
            if addr == SENTINEL or addr == stop_lin:
                self.ended = True
                uc.emu_stop()
            elif addr == 0xFFFF0:
                self.reset = True
                uc.emu_stop()
        mu.hook_add(UC_HOOK_CODE, code)
        init = dict(zip(NAMES, (0x1234, 0x0222, 0x5678, 0x00B5, 0x0444, 0x0566, 0x0E00, 0x0F00)))
        init.update(regs or {})
        for r, name in zip(_regs(), NAMES):
            if name in init:
                mu.reg_write(r, init[name])
        for r in (UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_SS):
            mu.reg_write(r, init.get({UC_X86_REG_DS: 'DS', UC_X86_REG_ES: 'ES', UC_X86_REG_SS: 'SS'}[r], 0))
        sp = init['SP']
        for w in reversed(stack):
            sp -= 2
            mu.mem_write(sp, (w & 0xFFFF).to_bytes(2, 'little'))
        mu.reg_write(UC_X86_REG_SP, sp)
        mu.reg_write(UC_X86_REG_CS, seg)
        for a, v in (ram or {}).items():
            mu.mem_write(a, bytes([v]))
        try:
            mu.emu_start((seg << 4) + (start & 0xFFFF), 0x200000, count=maxins)
        except UcError as e:
            self.err = str(e)
        self.regs = dict(zip(NAMES, (mu.reg_read(r) for r in _regs())))
        # RAM without the test stack (0E00h-0FFFh): a far call leaves its
        # return address below SP, which is not a difference for the game
        self.ram = bytes(mu.mem_read(0, 0xE00)) + bytes(mu.mem_read(0x1000, 0xF000))


def same(a, b, ignore=('FLAGS',), sel=True):
    if not (a.ended and b.ended) or b.reset:
        return False
    regs = all(a.regs[n] == b.regs[n] for n in NAMES if n not in ignore)
    return regs and a.ram == b.ram and (a.sel == b.sel or not sel) and a.outs == b.outs


_FLAGSET = {'and', 'or', 'xor', 'test', 'cmp', 'add', 'sub', 'inc', 'dec', 'neg'}


def _flag_ext(rom, e):
    """One more instruction when it rewrites the flags (and al, 0Fh...)."""
    if _MD is not None:
        for ins in _MD.disasm(rom[e:e + 8], e & 0xFFFF):
            if ins.mnemonic in _FLAGSET:
                e += ins.size
            break
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
    if _MD is not None:
        for ins in _MD.disasm(rom[start:site], start & 0xFFFF):
            if ins.mnemonic in ('ret', 'retf', 'iret', 'jmp', 'ljmp'):
                return site, 0x40
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
    """(verdict, lines). verdict: 'OK', 'FAIL' or 'UNVERIFIED' (no Unicorn,
    or no test could run)."""
    if not AVAILABLE:
        return 'UNVERIFIED', ['Unicorn not installed (pip install unicorn capstone): not checked']
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
