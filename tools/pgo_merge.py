"""Turn the PGO count streams of an instrumented NRO into .gcda files for the -DMANHUNT_PGO=use build.

The instrumented NRO (-DMANHUNT_PGO=generate, src/pgo_stream.c) writes sdmc:/switch/manhunt1_nx/pgo/manhunt_<run>.stream,
one per launch. Each stream holds, per object, a "gcfn" record with the .gcda name the instrumented build computed
(MANHUNT_PGO_DIR inside the toolchain container + the object's mangled path) followed by that object's .gcda data.

    python tools/pgo_merge.py <folder with .stream files> [--profile-dir game/build/pgo-data]

1. Copies the streams to game/build/pgo-streams/ (inside the repo, so the container sees them).
2. Runs `aarch64-none-elf-gcov-tool merge-stream` on each in the pinned devkitA64 image. Generate and use run in the same
   container path (/repo/game/build/switch-pgo), so the names need no rewriting, and the .gcda files land in
   game/build/pgo-data/. Streams are added together: run it on an empty profile folder, or on purpose on a used one.
3. Fixes each .gcda's object summary: the stream path leaves runs = 0 and sum_max = 0, and GCC derives its hot threshold
   from sum_max, so with 0 every block counts as hot. They become runs = <number of streams> and sum_max = the largest
   arc count of all objects.

The counts are derived from the game: they stay under game/build/ (gitignored).
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "devkitpro/devkita64@sha256:1fc388c3a0d34bd2045a6dadcb1020e069d5f876a187fd705de14b4440c00282"
GCDA_MAGIC = 0x67636461
TAG_OBJECT_SUMMARY = 0xA1000000
TAG_ARC_COUNTERS = 0x01A10000


def read_u32(data, pos):
    return struct.unpack_from("<I", data, pos)[0]


def records(data):
    """(tag, payload offset, payload length) of a .gcda (GCC 12+: lengths in bytes; an all-zero block has a negative
    length and no payload)."""
    pos = 16
    while pos + 8 <= len(data):
        tag, length = struct.unpack_from("<Ii", data, pos)
        if tag == 0:
            return
        size = max(length, 0)
        yield tag, pos + 8, size
        pos += 8 + size


def max_arc_count(path):
    data = path.read_bytes()
    best = 0
    for tag, at, size in records(data):
        if tag == TAG_ARC_COUNTERS and size:
            best = max(best, max(struct.unpack_from(f"<{size // 8}q", data, at)))
    return best


def fix_summary(path, runs, sum_max):
    data = bytearray(path.read_bytes())
    if len(data) < 16 or read_u32(data, 0) != GCDA_MAGIC:
        return "not a gcda"
    for tag, at, size in records(data):
        if tag == TAG_OBJECT_SUMMARY:
            old_runs, old_max = struct.unpack_from("<II", data, at)
            if old_runs == 0 and old_max == 0:
                struct.pack_into("<II", data, at, runs, min(sum_max, 0xFFFFFFFF))
                path.write_bytes(bytes(data))
                return "summary set"
            return "summary kept"
    return "no object summary"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("streams", type=Path)
    ap.add_argument("--profile-dir", type=Path, default=ROOT / "game/build/pgo-data")
    a = ap.parse_args()
    streams = sorted(a.streams.glob("*.stream"))
    if not streams:
        sys.exit(f"no .stream files in {a.streams}")
    a.profile_dir.mkdir(parents=True, exist_ok=True)   # gcov-tool merge-stream skips every object if it is missing
    work = ROOT / "game/build/pgo-streams"
    work.mkdir(parents=True, exist_ok=True)
    for s in streams:
        shutil.copyfile(s, work / s.name)
        print(f"{s.name}: {s.stat().st_size / 1048576:.1f} MB")
    script = "set -e; for s in /repo/game/build/pgo-streams/*.stream; do " \
             "/opt/devkitpro/devkitA64/bin/aarch64-none-elf-gcov-tool merge-stream \"$s\"; echo \"merged $(basename $s)\"; done"
    mount = f"type=bind,src={ROOT.as_posix()},dst=/repo"
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--mount", mount, IMAGE, "bash", "-c", script],
                       env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    if r.returncode:
        sys.exit("merge-stream failed")
    gcdas = sorted(a.profile_dir.rglob("*.gcda"))
    if not gcdas:
        sys.exit(f"no .gcda files appeared in {a.profile_dir}: check MANHUNT_PGO_DIR")
    sum_max = max(max_arc_count(g) for g in gcdas)
    results = {}
    for g in gcdas:
        results.setdefault(fix_summary(g, len(streams), sum_max), []).append(g.name)
    print(f"{len(gcdas)} .gcda files in {a.profile_dir}; largest arc count {sum_max}")
    for k, v in results.items():
        print(f"  {k}: {len(v)}")


if __name__ == "__main__":
    main()
