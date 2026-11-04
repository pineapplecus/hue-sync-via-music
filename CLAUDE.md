# hue-music-sync

Syncs Philips Hue lights to live music on a Mac. It listens to the system audio (through the BlackHole virtual
device) or a line-in, detects beats and deep bass, and drives the lights through the Hue Entertainment API.
Built for a party where a friend DJs; see "Presets" for the two light shows made so far.

## Quick start

```
./setup.sh                      # one time: installs everything, pairs the bridge, makes an Entertainment Area
./start.sh                      # run the default show (melissa-party)
./start.sh --preset noe-default
```

`setup.sh` is idempotent (safe to re-run) and installs Homebrew, BlackHole, the Python venv and the two Swift
helpers as needed. `./setup.sh --undo` restores the Mac's normal audio output.

## Layout

| File | What it is |
|---|---|
| `sync.py` | Everything in Python: CLI, Hue client, audio analysis, light show, streaming runner. Single file on purpose. |
| `hue_stream.swift` | Native helper. Opens the DTLS-PSK connection to the bridge (UDP 2100) and sends HueStream v2 packets. Python can't do DTLS, so this is a subprocess fed raw RGB frames on stdin. Built to `bin/hue_stream`. |
| `setup_audio.swift` | Creates/removes the "Hue Sync Output" Multi-Output Device (real speakers + BlackHole 2ch) via CoreAudio. Built to `bin/setup_audio`. |
| `setup.sh`, `start.sh` | Installer and launcher. |
| `tests/test_core.py` | Offline tests (synthetic audio, no hardware): `.venv/bin/python tests/test_core.py` |

`~/.hue-music-sync.json` holds the bridge ip, app key, streaming `clientkey` and `app_id`. It is a secret; never commit it.

## How it works

```
audio (BlackHole / line-in)
  -> Analyzer.process()  per 1024-sample block: beat (spectral flux), deep bass (low-passed energy jump), TempoTracker
  -> Show.trigger()/frame()  50x per second: one RGB value per Entertainment channel
  -> hue_stream (Swift, DTLS)  re-sends the NEWEST frame at 50 fps
  -> bridge -> lights
```

Two engines, chosen by `--engine auto|stream|rest`:

- **stream** (default when `pair` saved a clientkey): Entertainment API. ~20 ms delay, no queue, the lights can
  never fall behind the music or keep firing after you quit. Needs an Entertainment Area (`setup-area`).
- **rest**: one HTTPS command per change. Kept as a fallback only. The bridge accepts commands far faster than it can
  deliver them over Zigbee, so anything above ~5 commands/s queues up and the show lags and keeps playing after you
  stop. `Budget` (token bucket, `--max-rate`) and the deep-hit priority logic exist to limit that. Don't invest in this path.

Detection details worth knowing before touching `Analyzer`:

- **Beats**: spectral flux below 4 kHz against an adaptive threshold (`sens`, `std_weight`). Fires on hi-hats too, so the
  tempo hint filters anything closer than 0.7 beat.
- **Deep bass** (`an.deep`): 2-pass moving-average low-pass (~100 Hz) in the time domain, then "energy rose >= `deep_ratio`
  (1.4) x the quietest point of the last ~190 ms" AND >= 50% of the recent peak AND the bass dominates the block.
  Comparing with the recent *minimum* matters: sustained bass keeps a 1-second average high and hides real hits.
- **Tempo** (`TempoTracker`): autocorrelation of the flux over 8 s, scored at 1x/2x/4x the period, prior toward 128 BPM,
  a new tempo needs two agreeing estimates. Locks after ~5 s. Can still pick a 3:2 ratio on syncopated material.
  The live BPM drives the beat filter (`an.min_gap`, `show.plain_gap`). `--no-track` or `--bpm N` pins it.

## Presets (`PRESETS` in sync.py)

- **melissa-party** (default): `style: pulse`. Pink / lavender / champagne, soft brightness swells (no flashes),
  colors reach full strength quickly. Knobs: `palette`, `base_bri` (resting brightness), `attack`, `pulse_tau`
  (swell-in / fade-out seconds), `color_tau` (color change time), `peak_plain` / `peak_deep`.
- **noe-default**: `style: strobe`. Red pulses on beats; white blips only on deep bass hits (`white_ms` is hard-capped
  at 200 ms in code, white must never reach 0.25 s). Knobs: `base_bri`, `strobe_flashes`, `white_ms`, `white_gap_ms`,
  `strobe_cooldown`, `deep_ratio`.

Useful `run` flags: `--preset`, `--device` (index or name; default BlackHole), `--area`, `--every N` (pulse every Nth beat),
`--blend [SECONDS]` (slow color-to-color blend; off by default because it washes colors out), `--deep RATIO`
(lower = more white), `--bpm N`, `--no-track`, `--predict --lead MS` (experimental: starts pulses early when the beat is steady;
off by default, the user disliked it with the old REST engine and hasn't re-evaluated it with streaming), `--engine`.

## Other commands

`pair` (press the bridge button; saves the streaming key), `setup-area`, `areas`, `groups`, `lights`, `devices`,
`stream-test` (8 s red/green/blue/white, no audio), `meter` (5 s of levels, no lights), `record` (saves `capture.wav`
and prints a beat/deep-bass timeline; the way to debug "it isn't catching the bass").

## Gotchas learned the hard way

- **macOS microphone permission fails silently**: without it the terminal reads pure zeros (peak 0.000), no error.
  Allow the terminal app in System Settings > Privacy & Security > Microphone, then fully restart the terminal.
- **BlackHole** only appears after `sudo killall coreaudiod` (or a reboot). The app needs the system output to be the
  "Hue Sync Output" Multi-Output Device or BlackHole hears nothing; keyboard volume keys don't work on it.
- **Pairing**: the v1 pairing parameter is `generateclientkey` (no underscore). `generate_clientkey` returns
  "parameter not available". Pairing needs a physical press of the bridge button; re-pairing is the only way to get the
  streaming key (old pairings don't have one).
- **PSK identity** for DTLS is the `hue-application-id` response header of `GET https://<bridge>/auth/v1`, not the app key.
- Streaming needs the area to be started (`stream_action start`); a crashed run can leave it active, so `start_stream`
  sends a best-effort `stop` first. The helper is started with `start_new_session=True` so Ctrl+C reaches only Python,
  which then sends warm-white frames and stops the stream.
- The status-line `\r` redraw overwrites printed errors; errors are counted/printed on their own line.
- `hue ms` in the REST engine only measures HTTP acceptance, not delivery to the bulbs: it looked healthy while the bridge queue was seconds deep.
- The zsh used by the Claude Code Bash tool does not word-split unquoted `$var`; use `${=var}` or an array in test loops.

## Testing and what could not be tested

- `tests/test_core.py` covers detection, tempo, white-length cap and pulse smoothness with synthetic audio.
- The Claude Code sandbox blocks LAN and local sockets and gives no microphone access, so the DTLS handshake, the
  bridge REST calls and real audio capture were only ever verified by the user running things on their own machine
  (`stream-test`, `meter`, `record`). To debug detection, ask the user to run `sync.py record`, then analyze
  `capture.wav` offline (the file is readable from the sandbox).
- When changing the show, simulate `run_stream` with a fake `Hue`, a fake `sd.InputStream` and a stub helper script
  that `cat`s stdin to a file; check frame rate, white run lengths and `stop`/`start` calls.

## Conventions

Dependencies stay minimal (numpy, sounddevice, requests; Swift only for the two helpers). Presets are plain dicts;
new tuning knobs go in the preset with a comment, read with `pr.get(key, default)`. Keep `sync.py` Python 3.9-compatible.
