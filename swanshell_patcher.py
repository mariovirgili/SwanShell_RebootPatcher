#!/usr/bin/env python3
"""SwanShellPatcher - START + Y1 "back to the menu" patches for WonderSwan ROMs.

    python swanshell_patcher.py                      window (Tk)
    python swanshell_patcher.py ROM_OR_FOLDER... [options]

Options:
    -o, --out DIR      .Y1P files for SwanShellDX (default: ./PATCHES);
                       copy them to /NILESWAN/PATCHES on the SD card
    --rom DIR          also write a patched copy of each ROM in DIR
    --ips DIR          also write an IPS patch for each ROM in DIR
    --no-y1p           do not write .Y1P files
    --db DIR           more folders of .Y1P files to use (the bundled
                       patches/ folder is always used, unless --no-db)
    --no-db            do not use known patches (make everything again)
    --methods LIST     only these methods, comma separated:
                       database,classic,classic+,trampoline,port,inportb,
                       nopsled,cblock,vblank
    --caves            also put new code in long runs of 00h inside a bank
                       (may be graphics: test the game on the console)
    --port-all         port: try every known patch, not only the same game
    --all              try every method and report all that work
    --no-verify        do not run the emulated test (Unicorn)
    -q, --quiet        one line per ROM

The ROM files are only read. Exit code: 0 if every ROM got a patch.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(sys.argv[0] if not getattr(sys, 'frozen', False)
                                       else sys.executable))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swanpatch import __version__, emu                       # noqa: E402
from swanpatch.patcher import Options, patch_rom, save        # noqa: E402
from swanpatch.rom import ROM_EXTS                            # noqa: E402


def bundled_db():
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'patches')


def list_roms(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    if f.lower().endswith(ROM_EXTS):
                        out.append(os.path.join(root, f))
        elif os.path.isfile(p):
            out.append(p)
        else:
            print('!! not found: %s' % p)
    return out


def run(roms, opt, out, rom_dir, ips_dir, quiet=False, log=print):
    done = 0
    for path in roms:
        with open(path, 'rb') as f:
            data = f.read()
        res = patch_rom(data, os.path.basename(path), opt)
        name = os.path.basename(path)
        if res.ok:
            done += 1
            written = save(res, out, rom_dir, ips_dir)
            log('ok  %-48s %s.Y1P  %s %s' % (name[:48], res.key, res.method, res.verdict))
            if not quiet:
                for w in written:
                    log('      -> %s' % w)
        else:
            log('--  %-48s no patch' % name[:48])
        if not quiet or not res.ok:
            for l in res.lines:
                log('      ' + l)
    return done


def main(argv):
    if not argv:
        return gui()
    opt = Options(db_dirs=[bundled_db()])
    out, rom_dir, ips_dir, quiet, paths = os.path.join(os.getcwd(), 'PATCHES'), None, None, False, []
    it = iter(argv)
    for a in it:
        if a in ('-o', '--out'):
            out = next(it)
        elif a == '--rom':
            rom_dir = next(it)
        elif a == '--ips':
            ips_dir = next(it)
        elif a == '--no-y1p':
            out = None
        elif a == '--db':
            opt.db_dirs.insert(0, next(it))
        elif a == '--no-db':
            opt.use_db = False
        elif a == '--methods':
            opt.only = set(next(it).split(','))
        elif a == '--caves':
            opt.caves = True
        elif a == '--port-all':
            opt.port_all = True
        elif a == '--all':
            opt.everything = True
        elif a == '--no-verify':
            opt.verify = False
        elif a in ('-q', '--quiet'):
            quiet = True
        elif a in ('-h', '--help'):
            print(__doc__)
            return 0
        elif a == '--version':
            print('SwanShellPatcher', __version__)
            return 0
        else:
            paths.append(a)
    if not opt.use_db:
        # the other versions' patches are still useful to port them
        pass
    roms = list_roms(paths)
    if not roms:
        print(__doc__)
        return 2
    if opt.verify and not emu.AVAILABLE:
        print('note: Unicorn is not installed, patches are not checked '
              '(pip install unicorn capstone)')
    done = run(roms, opt, out, rom_dir, ips_dir, quiet)
    print('%d of %d ROMs patched' % (done, len(roms)))
    return 0 if done == len(roms) else 1


# ------------------------------------------------------------------ window

def gui():
    try:
        import threading
        import tkinter as tk
        from tkinter import filedialog, scrolledtext
    except ImportError:
        print(__doc__)
        return 2
    root = tk.Tk()
    root.title('SwanShellPatcher %s - START + Y1' % __version__)
    roms = []
    outv = tk.StringVar(value=os.path.join(os.path.expanduser('~'), 'PATCHES'))
    y1p, rom, ips = tk.BooleanVar(value=True), tk.BooleanVar(value=False), tk.BooleanVar(value=False)
    usedb, caves, verify, allm = (tk.BooleanVar(value=True), tk.BooleanVar(value=False),
                                  tk.BooleanVar(value=emu.AVAILABLE), tk.BooleanVar(value=False))
    top = tk.Frame(root)
    top.pack(fill='x', padx=8, pady=6)
    lbl = tk.Label(top, text='No ROM selected', anchor='w')

    def add_files():
        roms.extend(filedialog.askopenfilenames(
            filetypes=[('WonderSwan ROM', '*.ws *.wsc *.pc2'), ('All files', '*.*')]))
        lbl.config(text='%d ROM(s)' % len(roms))

    def add_dir():
        d = filedialog.askdirectory()
        if d:
            roms.extend(list_roms([d]))
            lbl.config(text='%d ROM(s)' % len(roms))

    def pick_out():
        d = filedialog.askdirectory()
        if d:
            outv.set(d)
    tk.Button(top, text='Add ROMs...', command=add_files).pack(side='left')
    tk.Button(top, text='Add folder...', command=add_dir).pack(side='left', padx=4)
    lbl.pack(side='left', padx=8)
    row = tk.Frame(root)
    row.pack(fill='x', padx=8)
    tk.Label(row, text='Output folder').pack(side='left')
    tk.Entry(row, textvariable=outv, width=60).pack(side='left', fill='x', expand=True, padx=4)
    tk.Button(row, text='...', command=pick_out).pack(side='left')
    opts = tk.Frame(root)
    opts.pack(fill='x', padx=8, pady=4)
    for text, var in (('.Y1P for SwanShellDX', y1p), ('patched ROM', rom), ('IPS', ips),
                      ('use known patches', usedb), ('code caves (00h)', caves),
                      ('emulated test', verify), ('try all methods', allm)):
        tk.Checkbutton(opts, text=text, variable=var).pack(side='left')
    log = scrolledtext.ScrolledText(root, width=110, height=30, font=('Courier', 9))
    log.pack(fill='both', expand=True, padx=8, pady=6)

    def say(s):
        log.after(0, lambda: (log.insert('end', s + '\n'), log.see('end')))

    def go():
        if not roms:
            say('Add some ROMs first.')
            return
        opt = Options(db_dirs=[bundled_db()], use_db=usedb.get(), caves=caves.get(),
                      verify=verify.get(), everything=allm.get())
        out = outv.get()
        args = (list(roms), opt, out if y1p.get() else None,
                os.path.join(out, 'ROM') if rom.get() else None,
                os.path.join(out, 'IPS') if ips.get() else None)

        def work():
            n = run(*args, log=say)
            say('== %d of %d ROMs patched. Copy the .Y1P files to /NILESWAN/PATCHES on the SD card.'
                % (n, len(args[0])))
        threading.Thread(target=work, daemon=True).start()
    tk.Button(root, text='Patch', command=go, width=20).pack(pady=(0, 8))
    if not emu.AVAILABLE:
        say('Unicorn is not installed: patches will not be checked (pip install unicorn capstone).')
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
