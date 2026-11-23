# SPDX-License-Identifier: GPL-3.0-or-later
# SwanShellPatcher - Copyright (C) 2026 Mario Virgili
"""A small interpreter of the WonderSwan's CPU (NEC V30MZ, 8086/80186
instruction set, real mode), enough to run a game's keypad routine and the
patched code around it.

Plain Python, no dependencies. Not cycle exact and not a full emulator: no
interrupts from hardware, no DMA, no prefetch queue. Ports are callbacks.
"""

CF, PF, AF, ZF, SF, TF, IF, DF, OF = 0x1, 0x4, 0x10, 0x40, 0x80, 0x100, 0x200, 0x400, 0x800
ES, CS, SS, DS = 0, 1, 2, 3
AX, CX, DX, BX, SP, BP, SI, DI = range(8)

_PARITY = [0] * 256
for _i in range(256):
    _PARITY[_i] = PF if bin(_i).count('1') % 2 == 0 else 0


class CPUError(Exception):
    pass


class CPU:
    """mem: bytearray of at least 1 MB (linear address space)."""

    def __init__(self, mem):
        self.m = mem
        self.r = [0] * 8              # AX CX DX BX SP BP SI DI
        self.s = [0] * 4              # ES CS SS DS
        self.ip = 0
        self.flags = 0x0002
        self.port_in = lambda port, size: 0
        self.port_out = lambda port, size, value: None
        self.stopped = False
        self.halted = False

    # ---------------------------------------------------------- memory
    def rb(self, seg, off):
        return self.m[((self.s[seg] << 4) + (off & 0xFFFF)) & 0xFFFFF]

    def rw(self, seg, off):
        return self.rb(seg, off) | (self.rb(seg, off + 1) << 8)

    def wb(self, seg, off, v):
        self.m[((self.s[seg] << 4) + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    def ww(self, seg, off, v):
        self.wb(seg, off, v)
        self.wb(seg, off + 1, v >> 8)

    def push(self, v):
        self.r[SP] = (self.r[SP] - 2) & 0xFFFF
        self.ww(SS, self.r[SP], v)

    def pop(self):
        v = self.rw(SS, self.r[SP])
        self.r[SP] = (self.r[SP] + 2) & 0xFFFF
        return v

    def fetch(self):
        v = self.rb(CS, self.ip)
        self.ip = (self.ip + 1) & 0xFFFF
        return v

    def fetchw(self):
        v = self.fetch()
        return v | (self.fetch() << 8)

    def fetchs8(self):
        v = self.fetch()
        return v - 256 if v & 0x80 else v

    @property
    def linear(self):
        return ((self.s[CS] << 4) + self.ip) & 0xFFFFF

    # ---------------------------------------------------------- registers
    def get8(self, i):
        return (self.r[i & 3] >> 8) & 0xFF if i & 4 else self.r[i] & 0xFF

    def set8(self, i, v):
        v &= 0xFF
        if i & 4:
            self.r[i & 3] = (self.r[i & 3] & 0x00FF) | (v << 8)
        else:
            self.r[i] = (self.r[i] & 0xFF00) | v

    def flag(self, f):
        return 1 if self.flags & f else 0

    def setf(self, f, on):
        if on:
            self.flags |= f
        else:
            self.flags &= ~f

    # ---------------------------------------------------------- ModRM
    def modrm(self):
        """(mod, reg, rm-operand) where the operand is ('r', index) or
        ('m', segment, offset)."""
        b = self.fetch()
        mod, reg, rm = b >> 6, (b >> 3) & 7, b & 7
        if mod == 3:
            return mod, reg, ('r', rm)
        r = self.r
        if mod == 0 and rm == 6:
            off, seg = self.fetchw(), DS
        else:
            off, seg = ((r[BX] + r[SI], DS), (r[BX] + r[DI], DS), (r[BP] + r[SI], SS),
                        (r[BP] + r[DI], SS), (r[SI], DS), (r[DI], DS), (r[BP], SS),
                        (r[BX], DS))[rm]
            if mod == 1:
                off += self.fetchs8()
            elif mod == 2:
                off += self.fetchw()
        if self.seg_ovr is not None:
            seg = self.seg_ovr
        return mod, reg, ('m', seg, off & 0xFFFF)

    def rd(self, op, w):
        if op[0] == 'r':
            return self.r[op[1]] if w else self.get8(op[1])
        return self.rw(op[1], op[2]) if w else self.rb(op[1], op[2])

    def wr(self, op, w, v):
        if op[0] == 'r':
            if w:
                self.r[op[1]] = v & 0xFFFF
            else:
                self.set8(op[1], v)
        elif w:
            self.ww(op[1], op[2], v)
        else:
            self.wb(op[1], op[2], v)

    # ---------------------------------------------------------- flags
    def szp(self, v, w):
        mask = 0xFFFF if w else 0xFF
        v &= mask
        f = self.flags & ~(ZF | SF | PF)
        if v == 0:
            f |= ZF
        if v & (0x8000 if w else 0x80):
            f |= SF
        f |= _PARITY[v & 0xFF]
        self.flags = f

    def alu(self, op, a, b, w):
        """add or adc sbb and sub xor cmp. Returns the result (cmp: a)."""
        mask, sign = (0xFFFF, 0x8000) if w else (0xFF, 0x80)
        if op in (0, 2):                                   # add, adc
            c = self.flag(CF) if op == 2 else 0
            res = a + b + c
            self.setf(CF, res > mask)
            self.setf(OF, ((a ^ res) & (b ^ res) & sign) != 0)
            self.setf(AF, ((a ^ b ^ res) & 0x10) != 0)
        elif op in (3, 5, 7):                              # sbb, sub, cmp
            c = self.flag(CF) if op == 3 else 0
            res = a - b - c
            self.setf(CF, res < 0)
            self.setf(OF, ((a ^ b) & (a ^ res) & sign) != 0)
            self.setf(AF, ((a ^ b ^ res) & 0x10) != 0)
        else:                                              # or, and, xor
            res = a | b if op == 1 else a & b if op == 4 else a ^ b
            self.flags &= ~(CF | OF | AF)
        res &= mask
        self.szp(res, w)
        return a if op == 7 else res

    def incdec(self, v, w, dec):
        mask, sign = (0xFFFF, 0x8000) if w else (0xFF, 0x80)
        res = (v - 1 if dec else v + 1) & mask
        self.setf(OF, res == (sign - 1 if dec else sign))
        self.setf(AF, ((v ^ res) & 0x10) != 0)
        self.szp(res, w)
        return res

    def shift(self, op, v, n, w):
        bits = 16 if w else 8
        mask, sign = (1 << bits) - 1, 1 << (bits - 1)
        if n == 0:
            return v
        cf = self.flag(CF)
        orig = v
        for _ in range(n):
            if op == 0:                                    # rol
                cf = 1 if v & sign else 0
                v = ((v << 1) | cf) & mask
            elif op == 1:                                  # ror
                cf = v & 1
                v = (v >> 1) | (cf << (bits - 1))
            elif op == 2:                                  # rcl
                nc = 1 if v & sign else 0
                v = ((v << 1) | cf) & mask
                cf = nc
            elif op == 3:                                  # rcr
                nc = v & 1
                v = (v >> 1) | (cf << (bits - 1))
                cf = nc
            elif op in (4, 6):                             # shl/sal
                cf = 1 if v & sign else 0
                v = (v << 1) & mask
            elif op == 5:                                  # shr
                cf = v & 1
                v >>= 1
            else:                                          # sar
                cf = v & 1
                v = (v >> 1) | (v & sign)
        self.setf(CF, cf)
        if op in (0, 1, 2, 3):
            if op in (0, 2):
                self.setf(OF, ((v & sign) != 0) != bool(cf))
            else:
                self.setf(OF, ((v ^ (v << 1)) & sign) != 0)
        else:
            if op in (4, 6):
                self.setf(OF, ((v & sign) != 0) != bool(cf))
            elif op == 5:
                self.setf(OF, (orig & sign) != 0)
            else:
                self.setf(OF, False)
            self.szp(v, w)
        return v

    def cond(self, c):
        f = self.flags
        z, cy, s, o, p = bool(f & ZF), bool(f & CF), bool(f & SF), bool(f & OF), bool(f & PF)
        t = (o, cy, z, cy or z, s, p, s != o, z or (s != o))[c >> 1]
        return (not t) if c & 1 else t

    # ---------------------------------------------------------- execute
    def jump_rel(self, d):
        self.ip = (self.ip + d) & 0xFFFF

    def interrupt(self, n):
        self.push(self.flags)
        self.push(self.s[CS])
        self.push(self.ip)
        self.flags &= ~(IF | TF)
        self.ip = self.m[n * 4] | self.m[n * 4 + 1] << 8
        self.s[CS] = self.m[n * 4 + 2] | self.m[n * 4 + 3] << 8

    def string_op(self, op, rep):
        w = op & 1
        step = (2 if w else 1) * (-1 if self.flags & DF else 1)
        src = self.seg_ovr if self.seg_ovr is not None else DS
        r = self.r
        while True:
            if rep and r[CX] == 0:
                return
            base = op & 0xFE
            if base == 0xA4:                               # movs
                v = self.rw(src, r[SI]) if w else self.rb(src, r[SI])
                (self.ww if w else self.wb)(ES, r[DI], v)
                r[SI] = (r[SI] + step) & 0xFFFF
                r[DI] = (r[DI] + step) & 0xFFFF
            elif base == 0xA6:                             # cmps
                a = self.rw(src, r[SI]) if w else self.rb(src, r[SI])
                b = self.rw(ES, r[DI]) if w else self.rb(ES, r[DI])
                self.alu(7, a, b, w)
                r[SI] = (r[SI] + step) & 0xFFFF
                r[DI] = (r[DI] + step) & 0xFFFF
            elif base == 0xAA:                             # stos
                (self.ww if w else self.wb)(ES, r[DI], r[AX] if w else r[AX] & 0xFF)
                r[DI] = (r[DI] + step) & 0xFFFF
            elif base == 0xAC:                             # lods
                if w:
                    r[AX] = self.rw(src, r[SI])
                else:
                    self.set8(0, self.rb(src, r[SI]))
                r[SI] = (r[SI] + step) & 0xFFFF
            elif base == 0xAE:                             # scas
                b = self.rw(ES, r[DI]) if w else self.rb(ES, r[DI])
                self.alu(7, r[AX] if w else r[AX] & 0xFF, b, w)
                r[DI] = (r[DI] + step) & 0xFFFF
            elif base == 0x6C:                             # ins
                v = self.port_in(r[DX], 2 if w else 1)
                (self.ww if w else self.wb)(ES, r[DI], v)
                r[DI] = (r[DI] + step) & 0xFFFF
            elif base == 0x6E:                             # outs
                v = self.rw(src, r[SI]) if w else self.rb(src, r[SI])
                self.port_out(r[DX], 2 if w else 1, v)
                r[SI] = (r[SI] + step) & 0xFFFF
            if not rep:
                return
            r[CX] = (r[CX] - 1) & 0xFFFF
            if base in (0xA6, 0xAE):
                z = bool(self.flags & ZF)
                if (rep == 0xF3 and not z) or (rep == 0xF2 and z):
                    return
            if r[CX] == 0:
                return

    def step(self):
        self.seg_ovr = None
        rep = 0
        while True:
            op = self.fetch()
            if op in (0x26, 0x2E, 0x36, 0x3E):
                self.seg_ovr = (op >> 3) & 3
            elif op in (0xF2, 0xF3):
                rep = op
            elif op == 0xF0:
                pass
            else:
                break
        r = self.r
        # ALU: 00-3F (op*8 + 0..5)
        if op < 0x40 and (op & 7) < 6:
            a_op, form = op >> 3, op & 7
            if form < 4:
                w = form & 1
                _, reg, rm = self.modrm()
                if form < 2:
                    res = self.alu(a_op, self.rd(rm, w), r[reg] if w else self.get8(reg), w)
                    if a_op != 7:
                        self.wr(rm, w, res)
                else:
                    res = self.alu(a_op, r[reg] if w else self.get8(reg), self.rd(rm, w), w)
                    if a_op != 7:
                        if w:
                            r[reg] = res
                        else:
                            self.set8(reg, res)
            else:
                w = form & 1
                imm = self.fetchw() if w else self.fetch()
                res = self.alu(a_op, r[AX] if w else r[AX] & 0xFF, imm, w)
                if a_op != 7:
                    if w:
                        r[AX] = res
                    else:
                        self.set8(0, res)
            return
        if op in (0x06, 0x0E, 0x16, 0x1E):
            self.push(self.s[op >> 3])
        elif op in (0x07, 0x17, 0x1F):
            self.s[op >> 3] = self.pop()
        elif op == 0x27 or op == 0x2F:                     # daa / das
            al, cf, af = r[AX] & 0xFF, self.flag(CF), self.flag(AF)
            old = al
            if op == 0x27:
                if (al & 0x0F) > 9 or af:
                    al += 6
                    af = 1
                if old > 0x99 or cf:
                    al += 0x60
                    cf = 1
            else:
                if (al & 0x0F) > 9 or af:
                    al -= 6
                    af = 1
                if old > 0x99 or cf:
                    al -= 0x60
                    cf = 1
            self.set8(0, al)
            self.setf(CF, cf)
            self.setf(AF, af)
            self.szp(al & 0xFF, 0)
        elif op == 0x37 or op == 0x3F:                     # aaa / aas
            al = r[AX] & 0xFF
            if (al & 0x0F) > 9 or self.flags & AF:
                if op == 0x37:
                    r[AX] = (r[AX] + 0x106) & 0xFFFF
                else:
                    r[AX] = (r[AX] - 0x106) & 0xFFFF
                self.setf(AF, True)
                self.setf(CF, True)
            else:
                self.setf(AF, False)
                self.setf(CF, False)
            self.set8(0, r[AX] & 0x0F)
        elif 0x40 <= op <= 0x4F:
            r[op & 7] = self.incdec(r[op & 7], 1, op >= 0x48)
        elif 0x50 <= op <= 0x57:
            v = r[op & 7]
            self.push(v)
        elif 0x58 <= op <= 0x5F:
            r[op & 7] = self.pop()
        elif op == 0x60:                                   # pusha
            sp = r[SP]
            for i in (AX, CX, DX, BX):
                self.push(r[i])
            self.push(sp)
            for i in (BP, SI, DI):
                self.push(r[i])
        elif op == 0x61:                                   # popa
            for i in (DI, SI, BP):
                r[i] = self.pop()
            self.pop()
            for i in (BX, DX, CX, AX):
                r[i] = self.pop()
        elif op == 0x68:
            self.push(self.fetchw())
        elif op == 0x6A:
            self.push(self.fetchs8() & 0xFFFF)
        elif op in (0x69, 0x6B):                           # imul r16, rm16, imm
            _, reg, rm = self.modrm()
            a = self.rd(rm, 1)
            b = self.fetchw() if op == 0x69 else self.fetchs8() & 0xFFFF
            a = a - 0x10000 if a & 0x8000 else a
            b = b - 0x10000 if b & 0x8000 else b
            res = a * b
            r[reg] = res & 0xFFFF
            big = not (-0x8000 <= res <= 0x7FFF)
            self.setf(CF, big)
            self.setf(OF, big)
        elif 0x6C <= op <= 0x6F or 0xA4 <= op <= 0xA7 or 0xAA <= op <= 0xAF:
            self.string_op(op, rep)
        elif 0x70 <= op <= 0x7F:
            d = self.fetchs8()
            if self.cond(op & 0x0F):
                self.jump_rel(d)
        elif 0x80 <= op <= 0x83:
            w = op & 1
            _, a_op, rm = self.modrm()
            imm = self.fetchw() if op == 0x81 else (self.fetchs8() & 0xFFFF if op == 0x83 else self.fetch())
            res = self.alu(a_op, self.rd(rm, w), imm, w)
            if a_op != 7:
                self.wr(rm, w, res)
        elif op in (0x84, 0x85):
            w = op & 1
            _, reg, rm = self.modrm()
            self.alu(4, self.rd(rm, w), r[reg] if w else self.get8(reg), w)
        elif op in (0x86, 0x87):
            w = op & 1
            _, reg, rm = self.modrm()
            a = self.rd(rm, w)
            b = r[reg] if w else self.get8(reg)
            self.wr(rm, w, b)
            if w:
                r[reg] = a
            else:
                self.set8(reg, a)
        elif 0x88 <= op <= 0x8B:
            w = op & 1
            _, reg, rm = self.modrm()
            if op < 0x8A:
                self.wr(rm, w, r[reg] if w else self.get8(reg))
            elif w:
                r[reg] = self.rd(rm, 1)
            else:
                self.set8(reg, self.rd(rm, 0))
        elif op == 0x8C:
            _, reg, rm = self.modrm()
            self.wr(rm, 1, self.s[reg & 3])
        elif op == 0x8D:
            _, reg, rm = self.modrm()
            if rm[0] != 'm':
                raise CPUError('lea with a register')
            r[reg] = rm[2]
        elif op == 0x8E:
            _, reg, rm = self.modrm()
            self.s[reg & 3] = self.rd(rm, 1)
        elif op == 0x8F:
            _, _, rm = self.modrm()
            self.wr(rm, 1, self.pop())
        elif 0x90 <= op <= 0x97:
            i = op & 7
            r[AX], r[i] = r[i], r[AX]
        elif op == 0x98:
            r[AX] = (r[AX] & 0xFF) | (0xFF00 if r[AX] & 0x80 else 0)
        elif op == 0x99:
            r[DX] = 0xFFFF if r[AX] & 0x8000 else 0
        elif op == 0x9A:
            off = self.fetchw()
            seg = self.fetchw()
            self.push(self.s[CS])
            self.push(self.ip)
            self.s[CS], self.ip = seg, off
        elif op == 0x9B:
            pass
        elif op == 0x9C:
            self.push(self.flags)
        elif op == 0x9D:
            self.flags = (self.pop() & 0x0FD5) | 0x0002
        elif op == 0x9E:
            self.flags = (self.flags & 0xFF00) | (((r[AX] >> 8) & 0xD5) | 0x02)
        elif op == 0x9F:
            self.set8(4, self.flags & 0xFF)
        elif 0xA0 <= op <= 0xA3:
            off = self.fetchw()
            seg = self.seg_ovr if self.seg_ovr is not None else DS
            if op == 0xA0:
                self.set8(0, self.rb(seg, off))
            elif op == 0xA1:
                r[AX] = self.rw(seg, off)
            elif op == 0xA2:
                self.wb(seg, off, r[AX])
            else:
                self.ww(seg, off, r[AX])
        elif op in (0xA8, 0xA9):
            w = op & 1
            imm = self.fetchw() if w else self.fetch()
            self.alu(4, r[AX] if w else r[AX] & 0xFF, imm, w)
        elif 0xB0 <= op <= 0xB7:
            self.set8(op & 7, self.fetch())
        elif 0xB8 <= op <= 0xBF:
            r[op & 7] = self.fetchw()
        elif op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3):
            w = op & 1
            _, s_op, rm = self.modrm()
            n = self.fetch() if op in (0xC0, 0xC1) else (1 if op in (0xD0, 0xD1) else r[CX] & 0xFF)
            self.wr(rm, w, self.shift(s_op, self.rd(rm, w), n, w))
        elif op in (0xC2, 0xC3):
            n = self.fetchw() if op == 0xC2 else 0
            self.ip = self.pop()
            r[SP] = (r[SP] + n) & 0xFFFF
        elif op in (0xC4, 0xC5):
            _, reg, rm = self.modrm()
            if rm[0] != 'm':
                raise CPUError('les/lds with a register')
            r[reg] = self.rw(rm[1], rm[2])
            self.s[ES if op == 0xC4 else DS] = self.rw(rm[1], rm[2] + 2)
        elif op in (0xC6, 0xC7):
            w = op & 1
            _, _, rm = self.modrm()
            self.wr(rm, w, self.fetchw() if w else self.fetch())
        elif op == 0xC8:                                   # enter
            size, level = self.fetchw(), self.fetch() & 0x1F
            self.push(r[BP])
            frame = r[SP]
            for i in range(1, level):
                r[BP] = (r[BP] - 2) & 0xFFFF
                self.push(self.rw(SS, r[BP]))
            if level:
                self.push(frame)
            r[BP] = frame
            r[SP] = (r[SP] - size) & 0xFFFF
        elif op == 0xC9:                                   # leave
            r[SP] = r[BP]
            r[BP] = self.pop()
        elif op in (0xCA, 0xCB):
            n = self.fetchw() if op == 0xCA else 0
            self.ip = self.pop()
            self.s[CS] = self.pop()
            r[SP] = (r[SP] + n) & 0xFFFF
        elif op == 0xCC:
            self.interrupt(3)
        elif op == 0xCD:
            self.interrupt(self.fetch())
        elif op == 0xCE:
            if self.flags & OF:
                self.interrupt(4)
        elif op == 0xCF:
            self.ip = self.pop()
            self.s[CS] = self.pop()
            self.flags = (self.pop() & 0x0FD5) | 0x0002
        elif op == 0xD4:                                   # aam
            base = self.fetch()
            if base == 0:
                raise CPUError('aam 0')
            al = r[AX] & 0xFF
            r[AX] = ((al // base) << 8) | (al % base)
            self.szp(r[AX] & 0xFF, 0)
        elif op == 0xD5:                                   # aad
            base = self.fetch()
            r[AX] = ((r[AX] & 0xFF) + ((r[AX] >> 8) & 0xFF) * base) & 0xFF
            self.szp(r[AX], 0)
        elif op == 0xD6:                                   # salc
            self.set8(0, 0xFF if self.flags & CF else 0)
        elif op == 0xD7:                                   # xlat
            seg = self.seg_ovr if self.seg_ovr is not None else DS
            self.set8(0, self.rb(seg, r[BX] + (r[AX] & 0xFF)))
        elif 0xE0 <= op <= 0xE3:
            d = self.fetchs8()
            if op == 0xE3:
                if r[CX] == 0:
                    self.jump_rel(d)
            else:
                r[CX] = (r[CX] - 1) & 0xFFFF
                z = bool(self.flags & ZF)
                if r[CX] != 0 and (op == 0xE2 or (op == 0xE1 and z) or (op == 0xE0 and not z)):
                    self.jump_rel(d)
        elif op in (0xE4, 0xE5, 0xEC, 0xED):
            port = self.fetch() if op < 0xEC else r[DX]
            if op & 1:
                r[AX] = self.port_in(port, 2) & 0xFFFF
            else:
                self.set8(0, self.port_in(port, 1))
        elif op in (0xE6, 0xE7, 0xEE, 0xEF):
            port = self.fetch() if op < 0xEE else r[DX]
            self.port_out(port, 2 if op & 1 else 1, r[AX] if op & 1 else r[AX] & 0xFF)
        elif op == 0xE8:
            d = self.fetchw()
            self.push(self.ip)
            self.ip = (self.ip + d) & 0xFFFF
        elif op == 0xE9:
            d = self.fetchw()
            self.ip = (self.ip + d) & 0xFFFF
        elif op == 0xEA:
            off = self.fetchw()
            self.s[CS], self.ip = self.fetchw(), off
        elif op == 0xEB:
            self.jump_rel(self.fetchs8())
        elif op == 0xF4:
            self.halted = True
            self.stopped = True
        elif op == 0xF5:
            self.flags ^= CF
        elif op in (0xF6, 0xF7):
            w = op & 1
            _, g, rm = self.modrm()
            v = self.rd(rm, w)
            if g in (0, 1):                                # test
                self.alu(4, v, self.fetchw() if w else self.fetch(), w)
            elif g == 2:                                   # not
                self.wr(rm, w, ~v)
            elif g == 3:                                   # neg
                res = self.alu(5, 0, v, w)
                self.wr(rm, w, res)
                self.setf(CF, v != 0)
            elif g in (4, 5):                              # mul, imul
                if w:
                    a, b = r[AX], v
                    if g == 5:
                        a = a - 0x10000 if a & 0x8000 else a
                        b = b - 0x10000 if b & 0x8000 else b
                    res = (a * b) & 0xFFFFFFFF
                    r[AX], r[DX] = res & 0xFFFF, res >> 16
                    big = r[DX] != 0 if g == 4 else not (-0x8000 <= a * b <= 0x7FFF)
                else:
                    a, b = r[AX] & 0xFF, v
                    if g == 5:
                        a = a - 0x100 if a & 0x80 else a
                        b = b - 0x100 if b & 0x80 else b
                    res = (a * b) & 0xFFFF
                    r[AX] = res
                    big = (res >> 8) != 0 if g == 4 else not (-0x80 <= a * b <= 0x7F)
                self.setf(CF, big)
                self.setf(OF, big)
            else:                                          # div, idiv
                if v == 0:
                    raise CPUError('division by zero')
                if w:
                    num = (r[DX] << 16) | r[AX]
                    if g == 7:
                        num = num - (1 << 32) if num & 0x80000000 else num
                        v = v - 0x10000 if v & 0x8000 else v
                        q = int(num / v)
                        rem = num - q * v
                        if not -0x8000 <= q <= 0x7FFF:
                            raise CPUError('divide overflow')
                    else:
                        q, rem = divmod(num, v)
                        if q > 0xFFFF:
                            raise CPUError('divide overflow')
                    r[AX], r[DX] = q & 0xFFFF, rem & 0xFFFF
                else:
                    num = r[AX]
                    if g == 7:
                        num = num - 0x10000 if num & 0x8000 else num
                        v = v - 0x100 if v & 0x80 else v
                        q = int(num / v)
                        rem = num - q * v
                        if not -0x80 <= q <= 0x7F:
                            raise CPUError('divide overflow')
                    else:
                        q, rem = divmod(num, v)
                        if q > 0xFF:
                            raise CPUError('divide overflow')
                    r[AX] = ((rem & 0xFF) << 8) | (q & 0xFF)
        elif op == 0xF8:
            self.flags &= ~CF
        elif op == 0xF9:
            self.flags |= CF
        elif op == 0xFA:
            self.flags &= ~IF
        elif op == 0xFB:
            self.flags |= IF
        elif op == 0xFC:
            self.flags &= ~DF
        elif op == 0xFD:
            self.flags |= DF
        elif op == 0xFE:
            _, g, rm = self.modrm()
            if g > 1:
                raise CPUError('FE /%d' % g)
            self.wr(rm, 0, self.incdec(self.rd(rm, 0), 0, g == 1))
        elif op == 0xFF:
            _, g, rm = self.modrm()
            if g in (0, 1):
                self.wr(rm, 1, self.incdec(self.rd(rm, 1), 1, g == 1))
            elif g == 2:
                t = self.rd(rm, 1)
                self.push(self.ip)
                self.ip = t
            elif g == 3 or g == 5:
                if rm[0] != 'm':
                    raise CPUError('far call/jmp through a register')
                off, seg = self.rw(rm[1], rm[2]), self.rw(rm[1], rm[2] + 2)
                if g == 3:
                    self.push(self.s[CS])
                    self.push(self.ip)
                self.s[CS], self.ip = seg, off
            elif g == 4:
                self.ip = self.rd(rm, 1)
            elif g == 6:
                self.push(self.rd(rm, 1))
            else:
                raise CPUError('FF /7')
        else:
            raise CPUError('opcode %02X at %04X:%04X' % (op, self.s[CS], (self.ip - 1) & 0xFFFF))

    def run(self, max_steps, before=None):
        """Runs until stopped, halted or max_steps. before(linear) is called
        before each instruction; returning True stops there."""
        for _ in range(max_steps):
            if before is not None and before(self.linear):
                return True
            self.step()
            if self.stopped:
                return True
        return False


# -------------------------------------------------------------- decoding

_MODRM = set(range(0x00, 0x40)) - {0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F, 0x14, 0x15, 0x16,
                                   0x17, 0x1C, 0x1D, 0x1E, 0x1F, 0x24, 0x25, 0x26, 0x27, 0x2C, 0x2D,
                                   0x2E, 0x2F, 0x34, 0x35, 0x36, 0x37, 0x3C, 0x3D, 0x3E, 0x3F}
_MODRM |= {0x62, 0x63, 0x69, 0x6B} | set(range(0x80, 0x90)) | {0xC0, 0xC1, 0xC4, 0xC5, 0xC6, 0xC7}
_MODRM |= set(range(0xD0, 0xD4)) | set(range(0xD8, 0xE0)) | {0xF6, 0xF7, 0xFE, 0xFF}
_IMM8 = {0x04, 0x0C, 0x14, 0x1C, 0x24, 0x2C, 0x34, 0x3C, 0x6A, 0x6B, 0x80, 0x82, 0x83, 0xA8,
         0xC0, 0xC1, 0xC6, 0xCD, 0xD4, 0xD5, 0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7,
         0xEB} | set(range(0x70, 0x80)) | set(range(0xB0, 0xB8))
_IMM16 = {0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D, 0x68, 0x69, 0x81, 0xA0, 0xA1, 0xA2,
          0xA3, 0xA9, 0xC2, 0xC7, 0xCA, 0xE8, 0xE9} | set(range(0xB8, 0xC0))


def decode(code, i):
    """(length, kind) of the instruction at code[i]; kind is 'ret', 'jmp',
    'flags' (and or xor test cmp add sub inc dec neg), or 'other'.
    None when the bytes run out."""
    start = i
    try:
        while code[i] in (0x26, 0x2E, 0x36, 0x3E, 0xF0, 0xF2, 0xF3):
            i += 1
        op = code[i]
        i += 1
        reg = None
        if op in _MODRM:
            b = code[i]
            i += 1
            mod, reg, rm = b >> 6, (b >> 3) & 7, b & 7
            if mod == 0 and rm == 6:
                i += 2
            elif mod == 1:
                i += 1
            elif mod == 2:
                i += 2
        if op in _IMM8:
            i += 1
        if op in _IMM16:
            i += 2
        if op in (0x9A, 0xEA):
            i += 4
        if op == 0xC8:
            i += 3
        if op in (0xF6, 0xF7) and reg in (0, 1):
            i += 1 if op == 0xF6 else 2
        if i > len(code):
            return None
    except IndexError:
        return None
    if op in (0xC2, 0xC3, 0xCA, 0xCB, 0xCF):
        kind = 'ret'
    elif op in (0xE9, 0xEA, 0xEB) or (op == 0xFF and reg in (4, 5)):
        kind = 'jmp'
    elif (op < 0x40 and (op & 7) < 6 and (op >> 3) not in (2, 3)) or 0x40 <= op <= 0x4F \
            or (0x80 <= op <= 0x83 and reg not in (2, 3)) or op in (0x84, 0x85, 0xA8, 0xA9) \
            or (op in (0xF6, 0xF7) and reg in (0, 1, 3)) or (op in (0xFE, 0xFF) and reg in (0, 1)):
        kind = 'flags'
    else:
        kind = 'other'
    return i - start, kind
