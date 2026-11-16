"""Does a recorded take land at the second it was recorded at?

The whole feature is one claim: audio captured at t=N in the browser comes back
out of the mix at t=N. Everything else is UI. This builds a project whose only
voice is two tone bursts on the take lane and checks the silence between them.
"""
import json, subprocess, tempfile
from pathlib import Path

import pipeline


def _tone(path: Path, secs: float):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={secs}",
                    "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(path)],
                   check=True)


def _loud_spans(wav: Path):
    """Start times of audible regions, from ffmpeg's own silence detector."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(wav),
                        "-af", "silencedetect=n=-45dB:d=0.2", "-f", "null", "-"],
                       capture_output=True, text=True)
    ends = [float(l.split("silence_end:")[1].split("|")[0])
            for l in r.stderr.splitlines() if "silence_end:" in l]
    return ends


def main():
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "out" / "vo" / "takes").mkdir(parents=True)
        _tone(proj / "out/vo/takes/take-1.wav", 0.5)
        _tone(proj / "out/vo/takes/take-2.wav", 0.5)
        # clean off: a steady tone has no pauses, so the cleaner reads it as
        # "no voice" and leaves it out — cleaning has its own check below
        m = {"out": "out", "segments": [],
             "vo": {"mute_tts": True, "clean": False, "takes": [
                 {"file": "out/vo/takes/take-1.wav", "at": 1.0, "dur": 0.5},
                 {"file": "out/vo/takes/take-2.wav", "at": 4.0, "dur": 0.5}]}}
        pipeline.mix(proj, m)

        mixdown = proj / "out/mixdown.wav"
        assert mixdown.is_file(), "mix produced no file"
        # the tail take ends at 4.5s, so that is how long the mix must be
        dur = pipeline.duration(mixdown)
        assert abs(dur - 4.5) < 0.05, f"expected a 4.5s mix, got {dur:.2f}s"

        starts = _loud_spans(mixdown)
        assert len(starts) == 2, f"expected 2 bursts, found {len(starts)}: {starts}"
        for want, got in zip((1.0, 4.0), starts):
            assert abs(got - want) < 0.06, f"take placed at {got:.2f}s, wanted {want:.2f}s"

        # muting one must actually drop it from the bus
        m["vo"]["takes"][0]["mute"] = True
        pipeline.mix(proj, m)
        assert len(_loud_spans(mixdown)) == 1, "muted take still audible"

    print("takes land where they were recorded ✓")
    music_only()
    music_outlasts_voice()
    fit_renders_first()
    edited_line_refits()
    taildrop_menu()
    swapped_inputs_are_stale()


def music_only():
    """No script, no takes, just a bed: the mix runs the length of the cut."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "out").mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=black:s=64x64:d=3", str(proj / "out/preview.mp4")], check=True)
        _tone(proj / "bed.wav", 1.0)          # shorter than the cut: it loops
        m = {"out": "out", "segments": [], "vo": {"lines": []},
             "audio": {"music": {"src": "bed.wav", "gain_db": 0, "fade_in": 0, "fade_out": 0.1}}}
        pipeline.mix(proj, m)
        dur = pipeline.duration(proj / "out/mixdown.wav")
        assert abs(dur - 3.0) < 0.05, f"expected a 3.0s music-only mix, got {dur:.2f}s"
        r = subprocess.run(["ffmpeg", "-i", str(proj / "out/mixdown.wav"), "-af",
                            "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
        mean = float(r.stderr.split("mean_volume:")[1].split("dB")[0])
        assert mean > -30, f"music-only mix is near silent ({mean} dB)"
        # nothing at all to mix is still an error
        m["audio"] = {}
        try:
            pipeline.mix(proj, m)
        except RuntimeError:
            pass
        else:
            raise AssertionError("empty mix should refuse")
    print("music-only mix runs the length of the cut ✓")


def music_outlasts_voice():
    """The duck is keyed off the voice; a read that ends early must not end the bed with it."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "out").mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=black:s=64x64:d=3", str(proj / "out/preview.mp4")], check=True)
        _tone(proj / "out/voiceover.wav", 1.0)
        _tone(proj / "bed.wav", 1.0)
        m = {"out": "out", "segments": [], "vo": {},
             "audio": {"music": {"src": "bed.wav", "gain_db": 0, "fade_in": 0, "fade_out": 0.1, "duck_db": -6}}}
        pipeline.mix(proj, m)
        r = subprocess.run(["ffmpeg", "-ss", "1.5", "-t", "1", "-i", str(proj / "out/mixdown.wav"),
                            "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
        mean = float(r.stderr.split("mean_volume:")[1].split("dB")[0])
        assert mean > -30, f"music went silent after the voice ended ({mean} dB)"
    print("music plays on after the voice ends ✓")


def fit_renders_first():
    """Update runs fit before vo, so an edited line must get audio inside fit."""
    calls = []
    orig = pipeline.render_lines, pipeline.fit_pauses
    pipeline.render_lines = lambda *a, **k: calls.append("render") or 1
    pipeline.fit_pauses = lambda *a, **k: calls.append("fit") or {"changed": 0, "late": []}
    try:
        with tempfile.TemporaryDirectory() as d:
            pipeline.do_step(Path(d), {"out": "out", "segments": []}, "fit")
    finally:
        pipeline.render_lines, pipeline.fit_pauses = orig
    assert calls == ["render", "fit"], calls
    print("fit renders missing lines first ✓")



def edited_line_refits():
    """A re-worded line changes length, so Update must re-solve pauses, not just rebuild."""
    names = ("clips_pending", "_current", "_older_than", "_voice_adrift", "_vo_current")
    orig = {n: getattr(pipeline, n) for n in names}
    for n, v in zip(names, (False, True, False, False, False)):   # only the voice is stale
        setattr(pipeline, n, lambda *a, v=v, **k: v)
    try:
        m = {"out": "out", "segments": [], "vo": {"voice": "v.wav", "lines": [{"text": "hi", "beat": "hook"}]}}
        steps = pipeline.stale(Path("/nonexistent"), m)
    finally:
        for n, f in orig.items():
            setattr(pipeline, n, f)
    assert steps[:2] == ["fit", "vo"], steps
    print("an edited line re-fits before the voice rebuilds ✓")


def taildrop_menu():
    """Offline devices sort last, and only finished files are offered."""
    import server
    real = server.subprocess.run
    server.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=(
        "100.1.1.1\told-pc\toffline; last seen 9h ago\n100.1.1.2\tphone\t\n"))
    try:
        assert server.taildrop_targets() == [{"name": "phone", "online": True},
                                             {"name": "old-pc", "online": False}]
    finally:
        server.subprocess.run = real
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "out").mkdir()
        for f in ("out/reel.mp4", "cut.mp4", "notes.md", "caption.txt"):
            (d / f).write_text("x")
        assert server.exports(d, {"out": "out"}) == ["out/reel.mp4", "caption.txt", "cut.mp4"]
    print("taildrop menu ok")


def swapped_inputs_are_stale():
    """New data an html clip loads (a music RMS track, a capture) is worth re-rendering."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "c.html").write_text('<script src="data.js"></script><script type="module">import x from "./kit.js";</script>')
        (d / "data.js").write_text("1"); (d / "kit.js").write_text("1")
        r = {"html": "c.html", "secs": 1}
        a = pipeline.clip_sig(d, r)
        (d / "data.js").write_text("2")
        b = pipeline.clip_sig(d, r)
        (d / "kit.js").write_text("2")
        assert len({a, b, pipeline.clip_sig(d, r)}) == 3, "html clip ignored a script it loads"
    print("clip scripts count toward its signature ✓")


def muted_line_drops_out():
    """A muted TTS line goes silent in place; the next line keeps its time."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "out" / "vo").mkdir(parents=True)
        m = {"out": "out", "segments": [], "vo": {"voice": "v", "lines": [
            {"text": "one", "pause": 1000}, {"text": "two", "pause": 0}]}}
        for l in m["vo"]["lines"]:
            _tone(proj / "out/vo" / (pipeline.line_key(m, l) + ".wav"), 0.5)
        pipeline.build_vo(proj, m)
        vo_sig, mix_sig = pipeline._vo_sig(proj, m), pipeline._mix_sig(proj, m)

        m["vo"]["lines"][0]["mute"] = True
        assert pipeline.wants_mix(proj, m), "a muted line must force a mix"
        assert pipeline._vo_sig(proj, m) == vo_sig, "muting must not re-synthesize"
        assert pipeline._mix_sig(proj, m) != mix_sig, "muting must re-mix"
        def peak(wav, t0, secs):  # loudest sample in [t0, t0+secs), dB
            r = subprocess.run(["ffmpeg", "-ss", str(t0), "-t", str(secs), "-i", str(wav),
                                "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True)
            return float(r.stderr.split("max_volume:")[1].split("dB")[0])
        mixdown = proj / "out/mixdown.wav"
        pipeline.mix(proj, m)
        assert peak(mixdown, 0.05, 0.4) < -60, "muted line one still audible"
        # line two starts at 0.5s of line one + 1.0s pause, muted or not
        assert peak(mixdown, 1.55, 0.4) > -20, "line two lost or moved"
        m["vo"]["lines"][0]["mute"] = False
        pipeline.mix(proj, m)
        assert peak(mixdown, 0.05, 0.4) > -20, "unmuting did not bring line one back"
    print("a muted line goes silent and the next keeps its place ✓")


def noisy_take_cleans_up():
    """A quiet read under loud hiss: found, gated, brought to TTS level, not moved."""
    def level(wav, t0, secs):
        r = subprocess.run(["ffmpeg", "-ss", str(t0), "-t", str(secs), "-i", str(wav),
                            "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
        return float(r.stderr.split("mean_volume:")[1].split("dB")[0])
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        d = proj / "out/vo/takes"
        d.mkdir(parents=True)
        raw, hiss = d / "take-1.wav", d / "take-2.wav"
        # a 40ms click at 0.1s (the tap on ●), then "words" from 0.8s: a 220Hz
        # buzz 0.5s on / 0.3s off, all under hiss above 1kHz as loud as a phone room
        words = "between(t,0.1,0.14)+gte(t,0.8)*lt(t,2.9)*lt(mod(t-0.8,0.8),0.5)"
        subprocess.run(["ffmpeg", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "anoisesrc=d=4:c=white:a=0.1:r=48000",
                        "-f", "lavfi", "-i", "sine=frequency=220:duration=4:sample_rate=48000",
                        "-filter_complex",
                        f"[0:a]highpass=f=1000,asplit[h1][h2];[h2]anull[hiss];"
                        f"[1:a]volume='1.5*({words})':eval=frame[v];"
                        "[h1][v]amix=inputs=2:normalize=0[take]",
                        "-map", "[take]", "-ac", "1", "-c:a", "pcm_s16le", str(raw),
                        "-map", "[hiss]", "-ac", "1", "-c:a", "pcm_s16le", str(hiss)], check=True)
        m = {"out": "out"}

        a, b = pipeline.voice_span(raw)
        assert abs(a - 0.72) < 0.06, f"speech found at {a:.2f}s, wanted 0.72 (the click moved it?)"
        assert abs(b - 3.1) < 0.1, f"speech ends at {b:.2f}s, wanted 3.1"

        out = pipeline.clean_take(proj, m, {"file": "out/vo/takes/take-1.wav"})
        got = pipeline.lufs(out)
        assert got is not None and abs(got + 16) < 1, f"cleaned take at {got} LUFS, wanted -16"
        before = level(raw, 1.0, 0.3) - level(raw, 3.3, 0.6)
        after = level(out, 1.0, 0.3) - level(out, 3.3, 0.6)
        assert after - before > 15, f"hiss after the read only {after - before:.1f}dB quieter"
        assert abs(pipeline.voice_span(out)[0] - a) < 0.051, "cleaning shifted the take in time"
        assert pipeline.clean_take(proj, m, {"file": "out/vo/takes/take-1.wav"}) == out, "not cached"

        assert pipeline.voice_span(hiss) is None, "hiss alone read as a voice"
        assert pipeline.clean_take(proj, m, {"file": "out/vo/takes/take-2.wav"}) is None, \
            "a take with no voice must stay out of the mix"
    print("a noisy take is found, gated and levelled in place ✓")


def fricative_survives_the_gate():
    """An "s" after a word, in a quiet room: the gate must not shut on it, at any
    room setting. The voice band alone never saw it, and the gate cut it 10-35dB."""
    def level(wav, t0, secs, af="anull"):
        r = subprocess.run(["ffmpeg", "-ss", str(t0), "-t", str(secs), "-i", str(wav),
                            "-af", f"{af},volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True)
        return float(r.stderr.split("mean_volume:")[1].split("dB")[0])
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        d = proj / "out/vo/takes"
        d.mkdir(parents=True)
        raw = d / "take-1.wav"
        # "word" 0.5-1.0s and 1.6-2.1s (220Hz), each followed by a 150ms "s"
        # (noise 4-9kHz), over faint hiss
        subprocess.run(["ffmpeg", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "anoisesrc=d=3:c=white:a=0.002:r=48000",
                        "-f", "lavfi", "-i", "sine=frequency=220:duration=3:sample_rate=48000",
                        "-f", "lavfi", "-i", "anoisesrc=d=3:c=white:a=0.3:r=48000:seed=7",
                        "-filter_complex",
                        "[0:a]highpass=f=1000[h];"
                        "[1:a]volume='0.6*(between(t,0.5,1.0)+between(t,1.6,2.1))':eval=frame[v];"
                        "[2:a]highpass=f=4000,lowpass=f=9000,"
                        "volume='between(t,1.0,1.15)+between(t,2.1,2.25)':eval=frame[s];"
                        "[h][v][s]amix=inputs=3:normalize=0",
                        "-ac", "1", "-c:a", "pcm_s16le", str(raw)], check=True)
        dry = level(raw, 1.02, 0.11, "highpass=f=4000") - level(raw, 0.6, 0.3)   # "s" vs the word
        for room in (25, 50, 100):
            m = {"out": "out", "vo": {"tone": {"room": room, "comp": 0, "crisp": 0}}}
            out = pipeline.clean_take(proj, m, {"file": "out/vo/takes/take-1.wav"})
            wet = level(out, 1.02, 0.11, "highpass=f=4000") - level(out, 0.6, 0.3)
            assert wet - dry > -3, f"room {room}: the 's' lost {dry - wet:.1f}dB to the gate"
            gap = level(out, 1.3, 0.2) - level(out, 0.6, 0.3)
            assert gap < -20, f"room {room}: the pause is only {-gap:.1f}dB down"
        # de-ess dims the "s", not the word under it
        m["vo"]["tone"]["deess"] = 100
        out = pipeline.clean_take(proj, m, {"file": "out/vo/takes/take-1.wav"})
        soft = level(out, 1.02, 0.11, "highpass=f=4000") - level(out, 0.6, 0.3)
        assert wet - soft > 3, f"deess took only {wet - soft:.1f}dB off the 's'"
    print("an 's' after a word survives the gate, and de-ess softens it ✓")


def soft_ending_survives_a_tight_trim():
    """A take trimmed to its voice has under 10% room in it. The gate's floor
    then sat on soft speech and shut on the quiet end of the last word."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        d = proj / "out/vo/takes"
        d.mkdir(parents=True)
        raw = d / "take-1.wav"
        # 0.08s room, 2.6s of loud "word", a 0.3s "-ing" 20dB softer, 0.2s room
        subprocess.run(["ffmpeg", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "anoisesrc=d=3.18:c=pink:a=0.0005:r=48000",
                        "-f", "lavfi", "-i", "sine=frequency=220:duration=3.18:sample_rate=48000",
                        "-filter_complex",
                        "[1:a]volume='0.5*between(t,0.08,2.68)+0.05*between(t,2.68,2.98)':eval=frame[v];"
                        "[0:a][v]amix=inputs=2:normalize=0",
                        "-ac", "1", "-c:a", "pcm_s16le", str(raw)], check=True)
        m = {"out": "out", "vo": {"tone": {"room": 50, "comp": 0, "crisp": 0, "denoise": 0}}}
        out = pipeline.clean_take(proj, m, {"file": "out/vo/takes/take-1.wav"})
        def mean(wav, t0, secs):
            r = subprocess.run(["ffmpeg", "-ss", str(t0), "-t", str(secs), "-i", str(wav),
                                "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
            return float(r.stderr.split("mean_volume:")[1].split("dB")[0])
        lost = (mean(raw, 2.75, 0.2) - mean(raw, 1.0, 0.5)) - (mean(out, 2.75, 0.2) - mean(out, 1.0, 0.5))
        assert lost < 3, f"the soft end of the word lost {lost:.1f}dB to the gate"
    print("the soft end of a tightly trimmed take survives the gate ✓")


def dead_quiet_pause_has_no_click():
    """A browser with noise suppression on hands over pauses of pure digital
    silence. DeepFilterNet skips those and comes back wrong: on a real voice a
    tick 30ms ahead of the next word, on this buzz the next word 19dB down."""
    def peak(wav, t0, secs):
        r = subprocess.run(["ffmpeg", "-ss", str(t0), "-t", str(secs), "-i", str(wav),
                            "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
        return float(r.stderr.split("max_volume:")[1].split("dB")[0])
    if not pipeline.DFN.is_file():
        return print("no deep-filter binary: skipped")
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        d = proj / "out/vo/takes"
        d.mkdir(parents=True)
        raw = d / "take-1.wav"
        # two 0.5s "words" (a buzz with harmonics, recorded far from the mic),
        # dead silence before, between and after them
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "aevalsrc='0.05*(between(t,0.4,0.9)+between(t,1.5,2.0))*"
                        "(sin(2*PI*150*t)+0.5*sin(2*PI*300*t)+0.4*sin(2*PI*900*t)+0.3*sin(2*PI*2400*t))':d=2.3:s=48000",
                        "-ac", "1", "-c:a", "pcm_s16le", str(raw)], check=True)
        m = {"out": "out", "vo": {"tone": {"room": 0, "comp": 0, "crisp": 0}}}
        out = pipeline.clean_take(proj, m, {"file": "out/vo/takes/take-1.wav"})
        first, second = peak(out, 0.5, 0.3), peak(out, 1.6, 0.3)
        assert abs(first - second) < 3, f"the word after the pause came back {first - second:.0f}dB down"
        for word in (0.4, 1.5):
            tick = peak(out, word - 0.15, 0.1) - peak(out, word + 0.1, 0.3)
            assert tick < -30, f"a tick {-tick:.0f}dB under the word, ahead of it at {word}s"
    print("a dead-quiet pause comes back without a tick, the next word intact ✓")


if __name__ == "__main__":
    main()
    muted_line_drops_out()
    noisy_take_cleans_up()
    fricative_survives_the_gate()
    soft_ending_survives_a_tight_trim()
    dead_quiet_pause_has_no_click()
