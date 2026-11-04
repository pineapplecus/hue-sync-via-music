# hue-music-sync

Makes your Philips Hue lights pulse to music playing on your Mac. Needs a Hue Bridge and color lights.

## Install

```
git clone <this repo> && cd hue-music-sync
./setup.sh
```

It installs what it needs (Apple command line tools, Homebrew, the BlackHole audio driver, Python packages), routes
the Mac's audio so the app can hear it, pairs with your bridge (you press its button when asked) and creates a Hue
Entertainment Area. Nothing needs to be installed beforehand, and re-running it is safe.

## Run

Play music on your Mac, then:

```
./start.sh                          # pink / lavender / champagne soft pulses
./start.sh --preset noe-default     # red pulses, white strobes on deep bass
./start.sh --every 2                # calmer: every second beat
```

Ctrl+C stops it and sets the lights to warm white. `./setup.sh --undo` puts your Mac's sound output back to normal.

## If something is off

- **Lights don't react and the status line shows `peak 0.00`**: allow your terminal in System Settings, Privacy &
  Security, Microphone, then quit and reopen the terminal.
- **No sound from the Mac**: the output must be "Hue Sync Output" (the setup creates it). Set the volume on your speakers; the keyboard volume keys don't work with it.
- **Test the lights without music**: `.venv/bin/python sync.py stream-test`
- More detail for developers: see `CLAUDE.md`.
