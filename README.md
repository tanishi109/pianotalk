# PianoTalk

English | [日本語](README.ja.md)

PianoTalk records everything you play on your digital piano, automatically, and lets Claude read those recordings and play phrases back on the piano during a conversation. Use it for feedback on your playing, music-theory analysis, or asking Claude to continue a phrase you started.

- **Requirements**: macOS (uses CoreMIDI and launchd), Python 3.12+ (installed for you by uv), and the Claude desktop app or Claude Code.
- **Piano**: any digital piano with MIDI in/out (Bluetooth MIDI or USB MIDI). Tested on a Kawai CN201 over Bluetooth MIDI. Reports from other models, working or not, are welcome.
- **Privacy**: recordings are stored only on your Mac, in `~/PianoTalk/`. The only thing Claude receives is the text a tool returns (note names, timing, and so on) when Claude calls it during a conversation.
- **Language**: the menu bar and the tool messages are currently in Japanese. Claude reads them without trouble and answers in your language, but the menu items will look unfamiliar. This README gives an English gloss for each one.
- PianoTalk is an unofficial personal project. It is not affiliated with Anthropic or Kawai Musical Instruments.

It consists of two programs:

| Program | Role |
|---|---|
| **Recorder daemon** `pianotalk-recorder` | Started by launchd at login and stays running, recording your playing automatically |
| **MCP server** `pianotalk-mcp` | Runs over stdio. Installed in the Claude desktop app as an extension (.mcpb); can also be registered with Claude Code |

```
~/PianoTalk/
  config.toml                 settings (created on first run)
  recordings/YYYY-MM-DD/      recordings (YYYYMMDD-HHMMSS.mid)
  recordings/kept/            kept recordings (never auto-deleted)
  suggestions/                phrases Claude played
  logs/recorder.log           daemon log
  playing.lock                playback lock (created and expired automatically)
  recorder.json               recorder status, read by the MCP server (removed when the recorder quits)
```

## How it behaves

- **Doesn't take over the input**: CoreMIDI inputs can be shared, so PianoTalk runs alongside notation apps and other MIDI software.
- **When a take starts and ends**: recording starts at the first note-on. It ends once every key and the sustain pedal have been released for `silence_seconds` (3 s by default). Trailing silence is not saved.
- **What is recorded**: notes plus the sustain (CC64), sostenuto (CC66), and soft (CC67) pedals. If a pedal was already down when the take started, it is recorded at 0 s.
- **Takes that are discarded**: takes with fewer than `min_notes` notes or shorter than `min_duration_seconds`.
- **No recording during playback**: input is ignored while `play_notes` is playing and for `post_play_mute_seconds` afterwards, so pianos that echo received MIDI back don't create bogus takes.
- **Automatic reconnection**: when the piano is switched off and on, or Bluetooth drops and reconnects, the daemon rescans ports every 2 seconds and reconnects on its own.
- **Automatic cleanup**: recordings older than the retention period (30 days by default) are moved to the Trash at startup and every 24 hours after that. Anything in `kept/` is left alone.
- **Deletion**: every deletion moves files to the macOS Trash, so you can restore them with Finder's "Put Back".
- **While paused or playing back**: the daemon still tracks which keys and pedals are down, so releasing them during that time never leaves a take stuck open.
- **Tells Claude when nothing is being recorded**: if the recorder isn't running, is paused, or has no piano connected, `list_recordings` and `get_recording` start with a note saying so, and Claude passes it on to you instead of quietly reading old takes.
- **App Nap**: App Nap is disabled with `NSProcessInfo.beginActivity` (idle sleep is not prevented), and the launchd job uses `ProcessType=Interactive`.

## MCP tools

| Tool | What it does |
|---|---|
| `list_recordings(limit=20)` | Lists recordings, newest first: ID (file name), date/time, length, note count, range, and kept flag |
| `get_recording(id="latest", start_sec?, end_sec?)` | Returns a summary followed by events in time order. Notes struck within 40 ms are grouped on one line as a chord (note names like C4, gap since the previous strike, each note's length and velocity, and bar:beat when a BPM is set). Pedals get their own lines. Takes with more than 400 strikes are summarized section by section; read a section in detail with `start_sec`/`end_sec` |
| `play_notes(notes, bpm?, pedal?)` | Plays a list of notes (timed in beats) on the piano and saves it to `suggestions/` as a .mid file |
| `keep_recording(id)` | Moves a recording to `recordings/kept/` so it is never auto-deleted |
| `delete_recording(id)` | Moves a recording to the Trash |

Example `play_notes` input:
```json
{"bpm": 90,
 "notes": [{"note": "C4", "start": 0, "duration": 1, "velocity": 80},
           {"note": "E4", "start": 1, "duration": 1},
           {"note": "C3", "start": 0, "duration": 2, "velocity": 50}],
 "pedal": [{"start": 0, "end": 2}]}
```

## Setup

### 1. Python environment

PianoTalk uses [uv](https://docs.astral.sh/uv/), which installs Python 3.12 and all dependencies into a `.venv` inside the project folder.

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
```

```bash
git clone https://github.com/tanishi109/pianotalk.git
```

```bash
cd pianotalk && ~/.local/bin/uv sync
```

Run every command below from this `pianotalk` folder.

### 2. Connect the piano

For Bluetooth, open Audio MIDI Setup → MIDI Studio → Bluetooth and connect the piano. For USB, just plug in the cable. To check that the piano is visible:

```bash
.venv/bin/pianotalk-ports
```

With only one MIDI device connected, no configuration is needed: PianoTalk picks it automatically (ignoring the IAC Driver, network sessions, and PianoTalk's own virtual ports). With several MIDI devices, set `input_port` and `output_port` in `~/PianoTalk/config.toml` to part of the piano's port name (for example `"CN201"`). Matching is by substring and ignores case. The config file is created on first run. To create it now, start `.venv/bin/pianotalk-recorder` once and stop it with Ctrl-C.

> Bluetooth MIDI sometimes does not reconnect by itself after the Mac restarts or the piano is switched off and on. If that happens, click "Connect" again in Bluetooth settings. Once the connection is back, the daemon picks it up automatically.

### 3. Start the recorder

Choose one of the two ways below. Only one recorder runs at a time: if one is already running, starting another does nothing (the app says so).

#### A. PianoTalk Recorder.app (start it when you want to record)

```bash
./scripts/build-recorder-app.sh
```

This builds the app on your Mac (1–2 minutes) and, after asking, puts it in `/Applications` (or `~/Applications`). Open **PianoTalk Recorder** from Launchpad or Spotlight before you play; the piano-keys icon appears in the menu bar. Choose 終了 (Quit) from that menu, or just log out, to stop it; a take in progress is saved first. Nothing keeps running in the background.

The app is built and signed (ad hoc) on your own Mac, so Gatekeeper doesn't block it and no Apple developer account is involved. To update it, pull the repository and run the script again.

#### B. launchd (always running from login)

```bash
./launchd/install.sh
```

From then on the daemon starts at login. It restarts automatically only if it crashes; if you stop it normally (for example with "Quit" in the menu), it stays stopped.

| To… | Do this |
|---|---|
| Stop it | Menu bar icon → 終了 (Quit), or `launchctl kill TERM gui/$(id -u)/com.pianotalk.recorder` |
| Start it | `launchctl kickstart gui/$(id -u)/com.pianotalk.recorder` |
| Restart it (after changing settings) | `launchctl kickstart -k gui/$(id -u)/com.pianotalk.recorder` |
| Pause recording only | Menu → 録音を一時停止 (Pause recording) / 録音を再開 (Resume recording) |
| Check its state | `launchctl print gui/$(id -u)/com.pianotalk.recorder \| grep -m1 "state ="` |
| Read the log | `tail -f ~/PianoTalk/logs/recorder.log` (or menu → ログを開く, Open log) |
| Disable / re-enable autostart | `launchctl disable gui/$(id -u)/com.pianotalk.recorder` / `launchctl enable …` |
| Unregister | `./launchd/install.sh uninstall` (your recordings are kept) |

#### Menu bar icon

While the daemon runs, a piano-keys icon appears in the menu bar.

| Icon | Meaning |
|---|---|
| Keys (gray) | Piano not connected (waiting for it) |
| Keys | Connected and idle |
| Keys + red ● | Recording (the menu shows elapsed time and note count) |
| Keys + red ○ | All keys and pedals released; saving in a few seconds (the menu shows the countdown, and says so if the take is too short to keep) |
| Keys + green ✓ | Saved (for 3 seconds) |
| Keys + – | Too short, not saved (for 3 seconds; the menu shows why) |
| Speaker | Claude is playing (input ignored) |
| Pause | Recording paused |

The menu shows the last saved recording (click to reveal it in Finder), pause/resume, the recordings folder, the log, and Quit. When the piano isn't connected it also offers to open Audio MIDI Setup. If Bluetooth drops, open MIDI Studio from there and reconnect.

#### Listening back on the piano

The menu's ピアノで聴き返す ▸ (Play back on piano) submenu lists your 10 most recent recordings and Claude's 5 most recent phrases. Pick one to play it on the connected piano.

- While it plays, the icon turns into a speaker and the menu shows elapsed time and ■ 再生を停止 (Stop playback). There is no length limit and you can stop at any time.
- Nothing is recorded during playback or for about 1 second after it, so listening back never creates a new take.
- The submenu is disabled while the piano is disconnected or while you are recording. If Claude is playing, playback is refused and the menu says why (with an alert sound).
- For older files, use 録音フォルダを開く (Open recordings folder).

- If you can't find the icon, check that it isn't hidden in System Settings → Menu Bar (it is listed as "PianoTalk Recorder" for the app, and as "python3.12" when the recorder runs from launchd).
- To turn the icon off, set `[ui] menubar = false` in `config.toml` and restart the daemon.

### 4. Install the Claude desktop extension

Download `pianotalk-<version>.mcpb` from the [latest release](https://github.com/tanishi109/pianotalk/releases/latest) and double-click it (or use Settings → Extensions → Advanced settings → Install Extension…). Claude desktop sets up Python and the dependencies itself; if your Mac has no uv, it downloads one.

The install screen warns that the extension gets access to your data and that the developer is not verified by Anthropic. Claude desktop shows this for every extension installed from a file. PianoTalk only reads and writes `~/PianoTalk/` and talks to your MIDI devices; the source is in this repository.

To build the extension yourself instead:

```bash
.venv/bin/python scripts/build-mcpb.py
```

This writes `dist/pianotalk-<version>.mcpb`.

### 5. Register with Claude Code

Register it at user scope so every project can use it. Run this from the `pianotalk` folder:

```bash
claude mcp add --scope user pianotalk -- "$(pwd)/.venv/bin/pianotalk-mcp"
```

Without the `claude` command, you can instead add the same entry as for the desktop app (plus `"type": "stdio"`) to the top-level `"mcpServers"` in `~/.claude.json`. To check that it worked, open `/mcp` in a new session.

## Trying it without a piano (virtual MIDI ports)

### Automated tests

The tests create real CoreMIDI virtual ports and check:

- recording, discarding short takes, and that a held pedal keeps a take open
- sharing a port with another app, and reconnecting when a port appears later
- sending `play_notes`, and that the echo isn't recorded
- every MCP tool

```bash
uv run pytest -v
```

### Manual end-to-end run

1. Temporarily change `~/PianoTalk/config.toml` as below and restart the daemon (`launchctl kickstart -k ...`, or if it isn't registered, run `.venv/bin/pianotalk-recorder` in another terminal).
   ```toml
   input_port = "PianoTalk Fake Piano"
   output_port = "PianoTalk Monitor"
   ```
2. In another terminal, play the virtual "piano". The take is saved to `recordings/` a few seconds after it finishes.
   ```bash
   .venv/bin/pianotalk-fake-piano --repeat 2
   ```
3. Keep `.venv/bin/pianotalk-monitor` running in another terminal and ask Claude to "read what I just played and continue it". The monitor prints the notes `play_notes` sends.
4. When you're done, set `input_port` and `output_port` back to their previous values (`""` for automatic) and restart the daemon.

## Settings (`~/PianoTalk/config.toml`)

| Key | Default | Description |
|---|---|---|
| `midi.input_port` / `midi.output_port` | `""` (auto) | Port name (substring match). When empty, the only connected MIDI device is used |
| `recording.silence_seconds` | 3.0 | Seconds after all keys and pedals are released before a take ends |
| `recording.bpm` | 0 (unset) | When set, used as the MIDI file tempo and to show bar:beat positions |
| `recording.beats_per_bar` | 4 | Beats per bar |
| `recording.min_notes` / `min_duration_seconds` | 3 / 2.0 | Shorter takes are not saved |
| `recording.retention_days` | 30 | Days to keep recordings (0 disables auto-deletion) |
| `playback.default_bpm` | 100 | BPM for `play_notes` when none is given |
| `playback.post_play_mute_seconds` | 1.0 | Seconds to ignore input after playback |
| `playback.max_seconds` | 120 | Longest allowed single playback |
| `ui.menubar` | true | Show the menu bar icon |

## Project layout

```
pianotalk/config.py    settings and paths
pianotalk/midiutil.py  note names, MIDI writing, analyzing recordings into text
pianotalk/storage.py   ID lookup, keeping, trash, expiry cleanup
pianotalk/lock.py      playback lock
pianotalk/presence.py  recorder status file and the warnings built from it
pianotalk/daemon.py    recorder daemon (+ pianotalk-ports)
pianotalk/menubar.py   menu bar icon and menu
pianotalk/player.py    listening back from the menu (plays a .mid on the piano)
pianotalk/server.py    MCP server
pianotalk/devtools.py  fake piano and monitor
launchd/install.sh     launchd install/uninstall
app/                   py2app setup for PianoTalk Recorder.app
scripts/build-recorder-app.sh  builds and installs PianoTalk Recorder.app
mcpb/                  Claude desktop extension manifest and entry point
scripts/build-mcpb.py  builds dist/pianotalk-<version>.mcpb
tests/                 unit tests and virtual-port integration tests
```

## License

[MIT License](LICENSE)
