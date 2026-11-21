"""A/B test of two NROs on the console with no one playing: build an SD card kit, then compare the two logs.

  python tools/console_ab.py kit --a <base.nro> --b <new.nro> [--out _work/console-ab] [--script tests/emu/first_mission.pad]
      Writes <out>/SD/switch/manhunt1_nx/ with manhunt-a.nro, manhunt-b.nro and a manhunt.env for the test (the
      release settings, the [perf] report every 2 s, the scripted pad and an exit after --secs), plus
      manhunt.env.play (the release settings alone) and LEIA-ME.txt. Copy the SD folder onto the card root.
      Each build writes its own log: sdmc:/switch/manhunt1_nx/log-manhunt-a.txt and log-manhunt-b.txt
      (MANHUNT_LOG_FILE's {nro}). Game data is not copied: it must already be in switch/manhunt1_nx/game/.

  python tools/console_ab.py compare <log-manhunt-a.txt> <log-manhunt-b.txt>
      Prints, for samples with more than 20,000 vertices per frame (the gameplay part, as in the project's
      reports): median FPS, median guest-thread CPU, GL draw time per draw, and the share of slow frames.

A single pair of runs is noisy (the scripted path drifts, so the scenes differ a little). Run each build twice,
alternating (a, b, a, b), before trusting a difference under about 5 %.
"""
import argparse
import re
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = "switch/manhunt1_nx"
PLAY_ENV = ["RECOMP_SPAN_4G=1", "MANHUNT_GPU_BACKEND=2", "MANHUNT_AUDIO_APU=1", "MANHUNT_WIDESCREEN=1",
            "MANHUNT_LANGUAGE=spanish", "MANHUNT_AMBIENT_LIGHT=-1"]   # scripts/make-release.py, README
PERF = re.compile(r"\[perf\] ([\d.]+)s fps ([\d.]+) max (\d+)ms slow (\d+) \| per frame [\d.]+ms: draws ([\d.]+) "
                  r"verts ([\d.]+).*?\| gl draw ([\d.]+).*?cpu main \d+% guest (\d+)% gl (\d+)% audio \d+% apu (\d+)%")
README = """Teste A/B do Manhunt no Switch (sem jogar)

1. Copie a pasta SD/ deste kit para a raiz do cartao (ela so acrescenta arquivos em switch/manhunt1_nx/).
   Os arquivos do jogo precisam estar em switch/manhunt1_nx/game/ (DEFAULT.xbe, audio/, levels/).
   Se ja houver um manhunt.env seu nessa pasta, guarde uma copia antes: o do kit o substitui.
2. No Switch, segure R ao abrir qualquer jogo instalado (modo title takeover) para abrir o menu homebrew.
3. Abra manhunt-a.nro. Nao toque em nada: ele joga sozinho por {secs} s e volta ao menu.
4. Abra manhunt-b.nro e espere do mesmo jeito.
5. (Recomendado) Repita a e b mais uma vez. Cada rodada sobrescreve o log daquela versao, entao copie os
   logs para o PC entre a primeira e a segunda volta, se for repetir.
6. Copie para o PC: switch/manhunt1_nx/log-manhunt-a.txt e log-manhunt-b.txt
   e rode:  python tools/console_ab.py compare log-manhunt-a.txt log-manhunt-b.txt
7. Para voltar a jogar normalmente: renomeie manhunt.env.play para manhunt.env.

a = {a}
b = {b}
Mesmo relogio nas duas rodadas: mesmo modo (portatil ou dock) e carregador igual nas duas.
"""


def read_script(path):
    steps = [l.split("#", 1)[0].strip() for l in Path(path).read_text(encoding="utf-8").splitlines()]
    return ",".join(s for s in steps if s)


def kit(args):
    out = Path(args.out)
    app = out / "SD" / APP
    if app.exists():
        shutil.rmtree(out / "SD")
    app.mkdir(parents=True)
    shutil.copyfile(args.a, app / "manhunt-a.nro")
    shutil.copyfile(args.b, app / "manhunt-b.nro")
    test = PLAY_ENV + ["MANHUNT_PERF_LOG=2", "MANHUNT_LOG_SYNC=0", f"MANHUNT_LOG_FILE=sdmc:/{APP}/log-{{nro}}.txt",
                       f"MANHUNT_AUTO_EXIT_SECS={args.secs}", "MANHUNT_PAD_SCRIPT=" + read_script(args.script)]
    (app / "manhunt.env").write_text("\n".join(test) + "\n", encoding="utf-8", newline="\n")
    (app / "manhunt.env.play").write_text("\n".join(PLAY_ENV) + "\n", encoding="utf-8", newline="\n")
    (out / "LEIA-ME.txt").write_text(README.format(secs=args.secs, a=Path(args.a).name, b=Path(args.b).name),
                                     encoding="utf-8")
    print(f"kit: {out / 'SD'} (copy it to the card root), instructions in {out / 'LEIA-ME.txt'}")


def stats(path):
    rows = []
    for line in Path(path).read_text("utf-8", "replace").splitlines():
        m = PERF.search(line)
        if m:
            secs, fps, mx, slow, draws, verts, gl, guest, glt, apu = map(float, m.groups())
            rows.append(dict(fps=fps, slow=slow / max(secs * fps, 1), draws=draws, verts=verts, gl_per_draw=gl / draws
                             if draws else 0, guest=guest, glt=glt, apu=apu))
    heavy = [r for r in rows if r["verts"] > 20000]
    med = lambda k: round(statistics.median(r[k] for r in heavy), 3) if heavy else None
    return {"samples": len(rows), "heavy": len(heavy), "fps": med("fps"), "draws": med("draws"),
            "guest_cpu": med("guest"), "gl_cpu": med("glt"), "apu_cpu": med("apu"),
            "gl_ms_per_draw": med("gl_per_draw"), "slow_share": med("slow")}


def compare(args):
    a, b = stats(args.a), stats(args.b)
    if not a["heavy"] or not b["heavy"]:
        sys.exit(f"not enough gameplay samples (a: {a['heavy']}, b: {b['heavy']}); did the script reach the level?")
    print(f"{'':16}{'a':>10}{'b':>10}{'b vs a':>10}")
    for k in ("heavy", "fps", "draws", "guest_cpu", "gl_cpu", "apu_cpu", "gl_ms_per_draw", "slow_share"):
        x, y = a[k], b[k]
        rel = f"{(y - x) / x * 100:+.1f}%" if x else ""
        print(f"{k:16}{x:>10}{y:>10}{rel:>10}")
    print("\nfps up and guest_cpu down favour b. Compare draws too: more draws per frame means a heavier scene.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("kit")
    k.add_argument("--a", required=True)
    k.add_argument("--b", required=True)
    k.add_argument("--out", default=str(ROOT / "_work/console-ab"))
    k.add_argument("--script", default=str(ROOT / "tests/emu/first_mission.pad"))
    k.add_argument("--secs", type=int, default=240)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    args = ap.parse_args()
    kit(args) if args.cmd == "kit" else compare(args)


if __name__ == "__main__":
    main()
