# SwanShell RebootPatcher

**START + Y1 → back to the menu** for WonderSwan / WonderSwan Color games
on the [nileswan](https://github.com/asiekierka/nileswan) cartridge with the
**SwanShellDX** menu.

SwanShellDX can reset a running game into the menu when you hold
**START + Y1 (Y up)**. To do it, the game's own button-reading code must call
a tiny check (the *hook*). The menu patches most games by itself at launch;
for the others it needs a `.Y1P` patch file. This tool makes those files:
it looks at a ROM, finds how the game reads the keypad, writes the patch and
**checks it by running the game's code on an emulated CPU** before saving.

- Pure Python 3: nothing to compile, it runs on Windows, Linux and macOS
  (`python swanshell_patcher.py`). A ready-made Windows `.exe` is attached
  to the releases.
- The ROM files are **only read**: the patch is applied by the menu, in RAM,
  when the game starts. It can also write a patched copy or an IPS.
- 224 patches already made (most of the commercial library, translations and
  homebrew) are included in `patches/`.

## Use

    pip install unicorn capstone        # optional, for the emulated test
    python swanshell_patcher.py          # window
    python swanshell_patcher.py ROM_OR_FOLDER... [-o PATCHES]

Copy the `.Y1P` files to **`/NILESWAN/PATCHES/`** on the SD card and turn on
*Settings > System > Advanced > START+Y1 back to menu* in SwanShellDX.

| Option | |
|---|---|
| `-o DIR` | where the `.Y1P` files go (default `./PATCHES`) |
| `--rom DIR` | also write a patched copy of each ROM (other menus, emulators) |
| `--ips DIR` | also write an IPS patch |
| `--no-db` | do not use the bundled patches: make everything again |
| `--db DIR` | more folders of `.Y1P` files |
| `--methods a,b` | only these methods (see below) |
| `--caves` | also put new code in long runs of `00h` inside a bank (may be graphics: test the game) |
| `--all` | try every method and report all that work |
| `--no-verify` | skip the emulated test |
| `-q` | one line per ROM |

Example output:

    ok  One Piece - Niji no Shima Densetsu (English).wsc  011900970E0080.Y1P  nopsled OK
          nopsled    OK  NOP delay at 300631, 700631
                     nopsled 300598: none:= A:= B:= START:= Y1:= START+A:= X1:= Y2:= START+Y1:reset

## How a game is patched

The keypad is port `B5h`: the game writes the group (`10h` Y1-Y4, `20h`
X1-X4, `40h` START/A/B) and reads four bits back. The methods, tried in
this order:

| Method | Games | What it does |
|---|---|---|
| `database` | already patched | the variant of `patches/<key>.Y1P` whose original bytes match |
| `classic` | most | the same patch the menu makes at launch: the wait before the button read becomes a call to the hook |
| `classic+` | | the same, with the hook in any free space |
| `trampoline` | DAA delays, no delay, code after the read | the read and the instructions around it move to a small routine in free space that calls the hook |
| `port` | translations, revisions, expanded ROMs | a patch of another version of the game (same publisher and game id): the same code is looked for at another offset, the new code is moved if its place is no longer free |
| `inportb` | games written in C | the function `inportb(port)` (`mov dx,[bp+6] / in al,dx`) jumps to a copy that also checks START+Y1 when it reads port `B5h`, group `40h` |
| `nopsled` | long runs of NOPs used as a delay | the check and the reset are written in the NOPs themselves: no free space needed, works in banked code |
| `cblock` | C code copying keys bit by bit (`test/je/or`) | the block is rewritten in a few bytes, the check and the reset go in the bytes it frees |
| `anyread` | any other read (any group, `in al,dx`, `rep in`...) | the read moves to a routine that first runs the full check (select `40h`, START+Y1) and then selects the game's group again |
| `vblank` | demos that never read the keys | the check goes in the VBlank interrupt handler |

The hook (85 bytes) and the reset sequence are the same as SwanShellDX's
(`src/menu/launch/launch_patch.c`): unlock the nileswan power register, then
jump to the IPL, which starts the menu.

Free space: by default only bytes left over by the build tools (`FFh` runs
at the end of a bank, `00h` padding at the end of a bank). `--caves` also
uses long `00h` runs inside a bank: they are often empty graphics tiles, so a
patch made that way is marked *test it on the console*.

## The emulated test

With [Unicorn](https://www.unicorn-engine.org/) installed, every patch is
run before it is saved: the game's routine, original and patched, from the
group select before the read to the end of the patched bytes, holding none,
A, B, START, Y1, START+A, X1 and Y2. The patched run must end in exactly the
same state (registers, RAM, keypad group); with START+Y1 it must reach the
reset. Verdicts: `OK`, `FAIL` (not saved), `UNVERIFIED` (the test could not
run: kept only if nothing else works), `TRUSTED` (a bundled patch made by
hand and tested on the console).

It checks the CPU side only: always play a patched game for a minute on the
console.

## .Y1P format

    ; comment
    [Game name (variant)]
    crc=A1656BBB
    p=0EEE2F:E6B5909090E4B5:9A00FE00E09090      file offset : original : new

File name = `<publisher><game id><version><checksum><64 KB banks>.Y1P`
(from the ROM's last 16 bytes). One file can hold several variants (original,
translations sharing the same header): the menu applies the first whose
original bytes all match.

## Credits

SwanShellDX and this tool: Mario Virgili with Claude (Anthropic).
Part of the bundled patches were first made with ChatGPT's SwanY1P tool
and checked here. Keypad and hardware documentation: [WSdev wiki](https://ws.nesdev.org/).
No ROM is included in this repository.

---

## In italiano

Crea le patch START + Y1 (torna al menu) per SwanShellDX. Uso:
`python swanshell_patcher.py` (finestra) oppure
`python swanshell_patcher.py cartella_rom -o PATCHES`, poi copia i `.Y1P` in
`/NILESWAN/PATCHES/` sulla SD. Le ROM vengono solo lette. Con
`pip install unicorn capstone` ogni patch è provata su una CPU emulata prima
di essere salvata.
