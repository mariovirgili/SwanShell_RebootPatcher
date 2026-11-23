"""Tries the methods in order on one ROM and keeps the first patch that the
emulated test accepts (see methods.py and emu.py)."""
import os

from . import emu, methods
from .rom import (diff_records, has_header, rom_key, rom_crc, save_variant, make_ips)


class Options:
    def __init__(self, db_dirs=(), use_db=True, only=None, caves=False, verify=True,
                 everything=False, port_all=False):
        self.db_dirs = list(db_dirs)
        self.use_db = use_db
        self.only = only                 # None or a set of method names
        self.caves = caves
        self.verify = verify
        self.everything = everything     # keep trying: list every method that works
        self.port_all = port_all         # port: try every .Y1P, not only the same game id


class Result:
    def __init__(self, data, name):
        self.data, self.name = data, name
        self.key, self.crc = rom_key(data), rom_crc(data)
        self.patched = None              # bytes
        self.records = []
        self.method = None
        self.verdict = None              # OK / UNVERIFIED / TRUSTED
        self.lines = []                  # report
        self.found = []                  # (method, verdict) of every candidate

    @property
    def ok(self):
        return self.patched is not None


def _candidates(name, data, opt):
    if name == 'database':
        return (methods.database(data, opt.db_dirs) if opt.use_db else []), 'not used'
    if name == 'classic':
        return methods.classic(data)
    if name == 'classic+':
        return methods.classic_plus(data, opt.caves)
    if name == 'trampoline':
        return methods.trampoline(data, opt.caves)
    if name == 'port':
        got = methods.port(data, opt.db_dirs, opt.caves, opt.port_all)
        return got, 'no other version in the database'
    if name == 'inportb':
        return methods.inportb(data, opt.caves)
    if name == 'nopsled':
        return methods.nopsled(data)
    if name == 'cblock':
        return methods.cblock(data)
    if name == 'anyread':
        return methods.anyread(data, opt.caves)
    if name == 'vblank':
        return methods.vblank(data, opt.caves)
    raise ValueError(name)


def patch_rom(data, name, opt):
    res = Result(data, name)
    if not has_header(data):
        res.lines.append('not a WonderSwan ROM (no header at the end of the file)')
        return res
    fallback = None
    for mname in methods.ORDER:
        if opt.only and mname not in opt.only:
            continue
        try:
            cands, why = _candidates(mname, data, opt)
        except Exception as e:                      # a method must never stop the others
            cands, why = [], 'error: %s' % e
        if not cands:
            res.lines.append('%-10s -  %s' % (mname, why))
            continue
        for c in cands:
            recs = diff_records(data, c.data)
            if not recs:
                continue
            if opt.verify:
                verdict, lines = emu.verify(data, c.data, recs, c.tests)
            else:
                verdict, lines = 'UNVERIFIED', ['not checked (--no-verify)']
            if mname == 'database' and verdict != 'OK':
                verdict = 'TRUSTED'                 # made by hand / tested on the console
            res.found.append((mname, verdict))
            head = '%-10s %s  %s%s' % (mname, verdict, c.note, ('  [' + c.risk + ']') if c.risk else '')
            res.lines.append(head)
            res.lines += ['             ' + l for l in lines]
            if verdict == 'UNVERIFIED' and opt.verify and emu.AVAILABLE:
                # the test could not run (e.g. the read is reached through
                # far calls): keep it only if nothing better turns up
                if fallback is None:
                    fallback = (c, recs, mname)
                continue
            if res.patched is None and verdict in ('OK', 'TRUSTED', 'UNVERIFIED'):
                res.patched, res.records, res.method, res.verdict = c.data, recs, mname, verdict
                if c.risk:
                    res.lines.append('             note: %s - test it on the console' % c.risk)
            if res.patched is not None and not opt.everything:
                return res
    if res.patched is None and fallback is not None:
        c, recs, mname = fallback
        res.patched, res.records, res.method, res.verdict = c.data, recs, mname, 'UNVERIFIED'
        res.lines.append('             kept %s although the test could not run it: '
                         'test it on the console' % mname)
    return res


def save(res, outdir=None, rom_dir=None, ips_dir=None):
    """Writes what was asked; returns the paths written."""
    paths = []
    if not res.ok:
        return paths
    base = os.path.splitext(os.path.basename(res.name))[0]
    ext = os.path.splitext(res.name)[1] or '.ws'
    if outdir:
        paths.append(save_variant(outdir, res.name, res.data, res.records, res.method))
    if rom_dir:
        os.makedirs(rom_dir, exist_ok=True)
        p = os.path.join(rom_dir, base + ' (START+Y1)' + ext)
        with open(p, 'wb') as f:
            f.write(res.patched)
        paths.append(p)
    if ips_dir:
        os.makedirs(ips_dir, exist_ok=True)
        p = os.path.join(ips_dir, base + ' (START+Y1).ips')
        with open(p, 'wb') as f:
            f.write(make_ips(res.records))
        paths.append(p)
    return paths
