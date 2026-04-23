# Lucha Pinchadiscos 🎭🎵

**LP** — A single-deck DJ app for macOS with a luchador soul.

---

## Features

### Turntable
- **Rotating luchador mask** at the center of the virtual turntable
- **2-finger scroll** on the turntable → scratch
- **2-finger click + hold** → vinyl brake (4.5s to full stop)
- **2-finger click + drag up/down** → scratch while braking
- Release → speed and playback restore automatically

### Playback
- **▶ PLAY / ‖ PAUSE** — resumes from paused position
- **■ STOP** — vinyl brake → stops at current position; press again → jumps to START marker
- **◀◀ REV** — reverse playback toggle
- **↺ B LOOP** — 3-press live loop (set IN → set OUT+loop → cancel)
- **● GLITCH** — hold to glitch loop, release to exit

### Waveform
- **Drag cursor** left/right on waveform → scrub / scratch
- **Mouse wheel** → zoom in/out
- **Drag markers** directly on waveform:
  - `START` / `END` — playback range
  - `LOOP IN` / `LOOP OUT` — W LOOP range
- **W LOOP** button (bottom-right of waveform) — activates loop between LOOP IN/OUT markers
- **RESET** button (top-right of waveform) — resets all markers

### Tempo & Volume
- Tempo slider ±50% with **±0 reset button**
- Volume slider

### Audio
- **Audio device selection** via Settings menu (supports BlackHole, etc.)
- KORG nanoKONTROL2 MIDI support

### Track Slots
- 8 slots for loading tracks
- Drag & drop MP3/WAV files onto slots
- Save / Load project (File menu)

---

## Requirements

- macOS 11.0+
- Python 3.11 (for running from source)
- Dependencies: `miniaudio`, `sounddevice`, `soundfile`, `rtmidi`, `tkinterdnd2`, `numpy`

---

## Run from Source

```bash
pip install miniaudio sounddevice soundfile python-rtmidi tkinterdnd2 numpy
python dj_app.py
```

## Build Standalone App

```bash
pip install pyinstaller
bash build_app.sh
open dist/LuchaPinchadiscos.app
```

---

## MIDI Mapping (KORG nanoKONTROL2)

| Control | Function |
|---------|----------|
| Fader 1 | Volume |
| Knob 1 | Tempo ±50% |
| Knob 2 | Scratch (fine) |
| Knob 3 | Scratch (wide) |
| Knob 4 | Scratch (x-wide) |
| PLAY | Play / Pause |
| STOP | Stop / Head cue |
| CUE | REV (reverse) |
| REC (hold) | Glitch |
| CYCLE | B LOOP |
| Solo 1–8 | Track slot select |

---

*¡Ándale, pues!* 🤼‍♂️
