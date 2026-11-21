"""Runs the Switch NRO in Ryubing (Ryujinx fork) with a scripted pad and reports FPS and errors.

Diagnostic only: the emulator says nothing about console speed. Use it to catch crashes, hangs, new errors and
image regressions between two builds, not to prove a speed-up.

What it does:
  1. Optionally builds the NRO first (--build: scripts/game-build.sh switch through Git Bash and Docker).
  2. Prepares the emulated SD card of a private emulator copy (default _work/emu/ryubing-1.3.3, portable mode):
     sdmc:/switch/manhunt1_nx/game is a directory junction to your own game folder (--game, never copied), and
     sdmc:/switch/manhunt1_nx/manhunt.env is written from tests/emu/emulator.env plus the pad script, the auto exit
     and any --env lines. An existing manhunt.env is kept as manhunt.env.emu-backup and restored afterwards.
  3. Starts Ryujinx headless, follows its log and the port's log file, and stops on the port's auto exit
     ([MH-EXIT]), a crash ([MH-CRASH]), the emulator exiting, or the time limit (auto exit + --grace).
  4. Copies the logs (and frame dumps with --dump) to _work/emu-runs/<time>-<name>/, writes summary.json and prints
     the FPS (median over [perf] samples, all and above 20,000 vertices), error counts and the last guest lines.
  5. With --baseline <summary.json>, compares against an earlier run: result, new error kinds, FPS change.
Exit status: 0 when the run reached its auto exit without a crash (and, with --baseline, no regression), 1 otherwise,
3 for a setup error.

Usage (Git Bash or PowerShell, from the repo root):
  python tools/emu_run.py --game D:/path/manhunt1_xbe --script tests/emu/first_mission.pad [--build] [--secs 240]
  python tools/emu_run.py --game ... --script ... --baseline _work/emu-runs/<earlier>/summary.json
"""
import argparse
import datetime
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EMU = ROOT / "_work/emu/ryubing-1.3.3/Ryujinx.exe"
APP = "switch/manhunt1_nx"           # sd_layout.h: MANHUNT_SD_BASE
SD_APP = "sdmc:/" + APP
# Ryubing 1.3.3 headless: no profile picker, missing services answered instead of
# stopping, OpenGL backend, no vsync. --input-id-1 0 (keyboard) gives player 1 a controller; the pad script
# replaces the controller's input anyway (pad_switch.c).
EMU_ARGS = ["--no-gui", "--disable-docked-mode", "--skip-user-profiles-manager", "--ignore-controller-applet",
            "--ignore-missing-services", "--graphics-backend", "OpenGl", "--vsync-mode", "Unbounded",
            "--input-id-1", "0"]
CRASH = ("[MH-CRASH]", "Unhandled exception", "Guest stack trace", "InvalidMemoryRegionException")
ERROR_PATTERNS = {   # kind -> regex over the guest log; counted per run and compared with --baseline
    "crash": r"\[MH-CRASH\]",
    "watchdog_stall": r"\[MH-WD\] .*: pc \+",   # the watchdog's thread dump ("[MH-WD] thread up" is normal)
    "gl_error": r"GL error|glGetError|\[MH-GL\].*error",
    "magenta_texture": r"magenta|undecodable",
    "unresolved_icall": r"unresolved|no target for indirect",
    "contig_refused": r"contig.*refus",
    "audio_underrun": r"underruns \d+ \(\+[1-9]",   # new underruns in that period ("underruns <total> (+<new>)")
    "audio_silent": r"played 0/s",          # no audio consumed in that period (seen in Ryubing)
    "config_ignored": r"config line longer",
}
EMU_ERROR = re.compile(r"\|E\||\|C\|")   # Ryujinx error / critical log lines
# Host-side noise that says nothing about the port (no time zone archive; window focus and cursor).
EMU_NOISE = ("TimeZoneBinary", "the cursor")
PERF = re.compile(r"\[perf\] [\d.]+s fps ([\d.]+) .*?draws ([\d.]+) verts ([\d.]+)")
FPS10 = re.compile(r"\[fps\] ([\d.]+) fps")


def fail(msg):
    print("emu_run: setup:", msg, file=sys.stderr)
    sys.exit(3)


def read_script(path):
    """A pad script file: one step per line (<ms>:<button>[+<button>][:<hold_ms>]), # comments. Returns the
    comma-joined MANHUNT_PAD_SCRIPT value (pad_script.c, at most 512 steps; manhunt.env lines up to 4095 bytes)."""
    steps = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not re.fullmatch(r"\d+:[a-z+]+(:\d+)?", line):
            fail(f"{path}: bad step '{line}'")
        steps.append(line)
    if len(steps) > 512:
        fail(f"{path}: {len(steps)} steps, pad_script.c takes 512")
    value = ",".join(steps)
    if len(value) > 4000:
        fail(f"{path}: {len(value)} bytes, manhunt.env lines stop at 4095")
    return value


def link_game(sd_app, game):
    """sdmc:/switch/manhunt1_nx/game -> the user's game folder, as a junction (no copy). Never deletes real data:
    an existing real folder there is used as it is."""
    dst = sd_app / "game"
    if game is None:
        if not (dst / "DEFAULT.xbe").exists():
            fail(f"no game data: pass --game <folder with DEFAULT.xbe, audio/, levels/> (or put it in {dst})")
        return
    game = Path(game).resolve()
    if not (game / "DEFAULT.xbe").exists():
        fail(f"{game} has no DEFAULT.xbe")
    if dst.exists() or os.path.islink(dst):
        if os.path.isjunction(dst) if hasattr(os.path, "isjunction") else dst.is_symlink():
            os.rmdir(dst) if os.name == "nt" else dst.unlink()     # removes only the link, not its target
        else:
            print(f"note: {dst} is a real folder; using it, --game ignored")
            return
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(game)], capture_output=True, text=True)
        if r.returncode:
            fail(f"mklink /J failed: {r.stdout}{r.stderr}")
    else:
        dst.symlink_to(game, target_is_directory=True)


def bmp_luma(path):
    """Mean luminance (0-255) of an uncompressed 24/32-bit BMP, or None."""
    data = Path(path).read_bytes()
    if data[:2] != b"BM":
        return None
    (off,) = struct.unpack_from("<I", data, 10)
    w, h = struct.unpack_from("<ii", data, 18)
    bpp, comp = struct.unpack_from("<HI", data, 28)
    if bpp not in (24, 32) or comp not in (0, 3):
        return None
    step, row = bpp // 8, ((w * bpp + 31) // 32) * 4
    total, n = 0, 0
    for y in range(0, abs(h), 4):                     # every 4th row and pixel is plenty for a summary
        base = off + y * row
        for x in range(0, w, 4):
            b, g, r = data[base + x * step: base + x * step + 3]
            total += 0.299 * r + 0.587 * g + 0.114 * b
            n += 1
    return round(total / n, 2) if n else None


def summarize(guest_lines, emu_lines):
    perf = [(float(f), float(d), float(v)) for f, d, v in
            (m.groups() for m in map(PERF.search, guest_lines) if m)]
    fps10 = [float(m.group(1)) for m in map(FPS10.search, guest_lines) if m]
    heavy = [f for f, d, v in perf if v > 20000]
    errors = {k: sum(1 for l in guest_lines if re.search(p, l, re.I)) for k, p in ERROR_PATTERNS.items()}
    emu_err = [l.strip() for l in emu_lines if EMU_ERROR.search(l) and not any(n in l for n in EMU_NOISE)]
    sig = sorted({re.sub(r"^\S+ \|[EC]\| ", "", re.sub(r"0x[0-9a-fA-F]+|\d+", "#", l))[:160] for l in emu_err})
    return {
        "perf_samples": len(perf),
        "fps_median_all": round(statistics.median([p[0] for p in perf]), 1) if perf else None,
        "fps_median_heavy": round(statistics.median(heavy), 1) if heavy else None,
        "heavy_samples": len(heavy),
        "draws_median": round(statistics.median([p[1] for p in perf]), 0) if perf else None,
        "fps10_median": round(statistics.median(fps10), 1) if fps10 else None,
        "errors": errors,
        "emu_error_lines": len(emu_err),
        "emu_error_kinds": sig[:40],
        "inputs_pressed": sum(1 for l in guest_lines if "[MH-INPUT] press" in l),
        "levels": [l.split("[MH-LEVEL] ", 1)[1].strip() for l in guest_lines if "[MH-LEVEL] " in l],
        "per_level": per_level(guest_lines),
    }


def take_snapshot(save, dest):
    """Copy the emulated card's save folder without the Xbox disk partition images (Partition*.img, about 5 GB, rebuilt
    at boot), keeping only the newest game save (UDATA/<title>/<save id>/ with a .xsv): with several saves the title
    loads one of its own choosing, which was an older one in the first try (2026-11-21)."""
    shutil.copytree(save, dest, ignore=shutil.ignore_patterns("Partition*.img"))
    prune_saves(dest)


def prune_saves(dest):
    for title in (Path(dest) / "UDATA").glob("*"):
        slots = [d for d in title.iterdir() if d.is_dir() and any(d.glob("*.xsv"))]
        for old in sorted(slots, key=lambda d: max(f.stat().st_mtime for f in d.glob("*.xsv")))[:-1]:
            shutil.rmtree(old)


def per_level(guest_lines):
    """Median FPS, draws and vertices per frame of the [perf] samples taken while each level was loaded."""
    cur, rows = "boot", {}
    for l in guest_lines:
        if "[MH-LEVEL] " in l:
            cur = l.split("[MH-LEVEL] ", 1)[1].strip()
        m = PERF.search(l)
        if m:
            rows.setdefault(cur, []).append(tuple(map(float, m.groups())))
    med = lambda xs: round(statistics.median(xs), 1)
    return {lv: {"samples": len(r), "fps": med([x[0] for x in r]), "draws": med([x[1] for x in r]),
                 "verts": med([x[2] for x in r]), "draws_p90": round(sorted(x[1] for x in r)[int(len(r) * 0.9)], 0)}
            for lv, r in rows.items()}


def compare(cur, base, fps_tol):
    """Regressions of `cur` against `base` (both summary dicts). FPS changes are reported, never proof."""
    out = []
    if base.get("result") == "auto-exit" and cur.get("result") != "auto-exit":
        out.append(f"result {base.get('result')} -> {cur.get('result')}")
    for k, v in cur["errors"].items():
        if v and not base["errors"].get(k):
            out.append(f"new error kind {k} ({v} lines)")
    new_kinds = set(cur["emu_error_kinds"]) - set(base.get("emu_error_kinds", []))
    for k in sorted(new_kinds)[:10]:
        out.append(f"new emulator error: {k}")
    lost = [lv for lv in base.get("levels", []) if lv not in cur.get("levels", [])]
    if lost:
        out.append(f"levels reached by the baseline but not now: {', '.join(lost)}")
    for key in ("fps_median_heavy", "fps_median_all"):
        a, b = base.get(key), cur.get(key)
        if a and b and b < a * (1 - fps_tol):
            out.append(f"{key} {a} -> {b} (emulator FPS, diagnostic only)")
            break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--emu", default=str(DEFAULT_EMU), help="Ryujinx.exe of a portable copy (portable/ beside it)")
    ap.add_argument("--nro", default=str(ROOT / "game/build/switch/manhunt.nro"))
    ap.add_argument("--build", action="store_true", help="run scripts/game-build.sh switch first (Docker, Git Bash)")
    ap.add_argument("--jobs", type=int, default=4, help="compile jobs for --build (CPUS; default 4: Docker Desktop's VM "
                                                         "has about 7.3 GB here and the lifted chunks are large)")
    ap.add_argument("--game", help="your game folder: DEFAULT.xbe, audio/, levels/ (linked, never copied)")
    ap.add_argument("--script", help="pad script file (tests/emu/*.pad)")
    ap.add_argument("--secs", type=int, default=240, help="MANHUNT_AUTO_EXIT_SECS (default 240)")
    ap.add_argument("--grace", type=int, default=120, help="extra seconds before the run counts as hung (boot, lift)")
    ap.add_argument("--profile", default=str(ROOT / "tests/emu/emulator.env"))
    ap.add_argument("--env", action="append", default=[], help="extra KEY=VALUE for manhunt.env (repeatable)")
    ap.add_argument("--reset-save", action="store_true",
                    help="empty the emulated card's save folder first (sdmc:/switch/manhunt1_nx/save). Without it a run "
                         "continues from the save the previous run left, so two runs may start in different places")
    ap.add_argument("--save-snapshots", metavar="DIR",
                    help="copy the emulated card's save folder to DIR/<level> each time a new level starts loading "
                         "([MH-LEVEL]): the save that leads into that level, for --level runs later")
    ap.add_argument("--load-save", metavar="DIR", help="replace the emulated card's save folder with DIR before the run")
    ap.add_argument("--level", help="play one level: --load-save " + str(Path("_work/emu-saves")) + "/<LEVEL>, and "
                                    "--script tests/emu/first_mission.pad unless --script is given (it continues the "
                                    "loaded save, then walks). Snapshots come from a --save-snapshots run of levels.pad")
    ap.add_argument("--dump", type=int, default=0, metavar="N", help="save every Nth GL frame (RECOMP_GL_DUMP)")
    ap.add_argument("--name", help="label for the run folder (default: the script's name)")
    ap.add_argument("--out", default=str(ROOT / "_work/emu-runs"))
    ap.add_argument("--baseline", help="summary.json of an earlier run to compare with")
    ap.add_argument("--fps-tol", type=float, default=0.15, help="FPS drop that counts as a regression (default 15%%)")
    args = ap.parse_args()

    if args.build:
        # Git Bash, not WSL's System32\bash.exe (which shutil.which may find first on Windows).
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
        env = dict(os.environ, MSYS_NO_PATHCONV="1", CPUS=str(args.jobs))
        if args.game:
            env["XBE"] = str(Path(args.game).resolve() / "DEFAULT.xbe")
        print("building: scripts/game-build.sh switch")
        if subprocess.run([bash, "scripts/game-build.sh", "switch"], cwd=ROOT, env=env).returncode:
            print("result: build-failed")
            return 1
    emu, nro = Path(args.emu).resolve(), Path(args.nro).resolve()
    if not emu.exists():
        fail(f"no emulator at {emu}")
    if not (emu.parent / "portable").is_dir():
        fail(f"{emu.parent / 'portable'} missing: without it Ryujinx uses %APPDATA% instead of its own folder")
    if not nro.exists():
        fail(f"no NRO at {nro} (build it: --build)")
    sd_app = emu.parent / "portable/sdcard" / APP
    sd_app.mkdir(parents=True, exist_ok=True)
    link_game(sd_app, args.game)
    if args.level:
        args.load_save = args.load_save or str(ROOT / "_work/emu-saves" / args.level)
        args.script = args.script or str(ROOT / "tests/emu/first_mission.pad")
        args.name = args.name or f"level-{args.level}"
    if args.reset_save and (sd_app / "save").is_dir():
        shutil.rmtree(sd_app / "save")          # the emulator copy's own card only
        print("save folder emptied")
    if args.load_save:
        src = Path(args.load_save)
        if not src.is_dir():
            fail(f"no save snapshot {src} (make them with a --save-snapshots run of tests/emu/levels.pad)")
        shutil.rmtree(sd_app / "save", ignore_errors=True)
        shutil.copytree(src, sd_app / "save")
        print(f"save loaded from {src}")

    lines = [l for l in Path(args.profile).read_text(encoding="utf-8").splitlines() if l.strip()]
    log_name = "emu-run.log"
    lines += ["# emu_run.py", f"MANHUNT_LOG_FILE={SD_APP}/{log_name}", f"MANHUNT_AUTO_EXIT_SECS={args.secs}"]
    if args.script:
        lines.append("MANHUNT_PAD_SCRIPT=" + read_script(args.script))
    frames = sd_app / "frames/emu"
    if args.dump:
        shutil.rmtree(frames, ignore_errors=True)
        frames.mkdir(parents=True)
        lines.append(f"RECOMP_GL_DUMP={SD_APP}/frames/emu/f,{args.dump}")
    lines += args.env
    env_path, backup = sd_app / "manhunt.env", sd_app / "manhunt.env.emu-backup"
    had_env = env_path.exists()
    if had_env and not backup.exists():
        shutil.copyfile(env_path, backup)
    env_path.write_bytes(("\n".join(lines) + "\n").encode())
    (sd_app / log_name).unlink(missing_ok=True)

    logs_dir = emu.parent / "portable/Logs"
    before = set(logs_dir.glob("*.log")) if logs_dir.is_dir() else set()
    cmd = [str(emu), *EMU_ARGS, str(nro)]
    print("starting:", " ".join(cmd))
    t0 = time.time()
    limit = args.secs + args.grace
    result, reason, emu_log, pos, exit_seen = "timeout", f"no auto exit within {limit} s", None, 0, None
    emu_lines = []
    proc = subprocess.Popen(cmd, cwd=str(emu.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while time.time() - t0 < limit:
            time.sleep(2)
            if emu_log is None and logs_dir.is_dir():
                new = [p for p in logs_dir.glob("*.log") if p not in before]
                emu_log = max(new, key=lambda p: p.stat().st_mtime) if new else None
            if emu_log is not None:
                with open(emu_log, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                cut = chunk.rfind(b"\n") + 1
                pos += cut
                for raw in chunk[:cut].decode("utf-8", "replace").splitlines():
                    emu_lines.append(raw)
                    if "[MH-BOOT]" in raw or "[MH-INPUT]" in raw or "[perf]" in raw:
                        print("  " + raw.split("PrintGuestLog", 1)[-1].strip()[:150])
                    if args.save_snapshots and "[MH-LEVEL] " in raw and (sd_app / "save").is_dir():
                        lv = raw.split("[MH-LEVEL] ", 1)[1].strip()
                        snap = Path(args.save_snapshots) / lv
                        if not snap.exists():          # the first entry into a level is the one to keep
                            take_snapshot(sd_app / "save", snap)
                            print(f"  save snapshot for {lv}")
                    if "[MH-EXIT]" in raw and exit_seen is None:
                        exit_seen = time.time()
                        result, reason = ("auto-exit", raw.strip()) if "auto exit" in raw else ("exited", raw.strip())
                    if any(c in raw for c in CRASH):
                        result, reason = "crash", raw.strip()
            if result == "crash" or (exit_seen and time.time() - exit_seen > 8):
                break
            if proc.poll() is not None:
                if result == "timeout":
                    result, reason = "emulator-exited", f"Ryujinx exited with {proc.returncode} before the auto exit"
                break
    finally:
        if proc.poll() is None:           # only the process this run started
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if had_env:
            shutil.copyfile(backup, env_path)
            backup.unlink()
        else:
            env_path.unlink(missing_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = args.name or (Path(args.script).stem if args.script else "noscript")
    dest = Path(args.out) / f"{stamp}-{name}-{result}"
    dest.mkdir(parents=True)
    guest_lines = []
    if (sd_app / log_name).exists():
        shutil.copyfile(sd_app / log_name, dest / "guest.log")
        guest_lines = (dest / "guest.log").read_text("utf-8", "replace").splitlines()
    if emu_log is not None:
        shutil.copyfile(emu_log, dest / "ryujinx.log")
    if not guest_lines:   # the port's log file never opened: fall back to the guest lines in the emulator log
        guest_lines = [l for l in emu_lines if "[MH-" in l or "[perf" in l or "[fps]" in l]
    summary = {"result": result, "reason": reason, "seconds": round(time.time() - t0), "nro": str(nro),
               "nro_size": nro.stat().st_size, "script": args.script, "env": lines, **summarize(guest_lines, emu_lines)}
    if args.dump and frames.is_dir():
        shutil.copytree(frames, dest / "frames")
        summary["frames"] = {p.name: bmp_luma(p) for p in sorted((dest / "frames").glob("*.bmp"))}
    regressions = []
    if args.baseline:
        base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        regressions = compare(summary, base, args.fps_tol)
        summary["baseline"], summary["regressions"] = args.baseline, regressions
    (dest / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    print(f"\nresult: {result}: {reason}")
    print(f"FPS (emulator, diagnostic): median {summary['fps_median_all']} over {summary['perf_samples']} samples, "
          f"{summary['fps_median_heavy']} over {summary['heavy_samples']} heavy samples (>20k verts)")
    if summary["levels"]:
        print("levels loaded:", " -> ".join(summary["levels"]))
    print("errors:", ", ".join(f"{k} {v}" for k, v in summary["errors"].items() if v) or "none",
          f"| emulator error lines {summary['emu_error_lines']}")
    for l in [l for l in guest_lines if "[MH-CRASH]" in l][:8]:
        print("  " + l.strip())
    for r in regressions:
        print("REGRESSION:", r)
    print("run folder:", dest)
    return 0 if result == "auto-exit" and not regressions else 1


if __name__ == "__main__":
    sys.exit(main())
