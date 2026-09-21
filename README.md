# Jarvis

A small personal assistant I run in the morning. Boots with a spoken
neural greeting, opens Spotify + Google, then waits for typed (or
spoken) commands.

Everything runs locally. No cloud accounts, no API keys — the neural
voice uses Microsoft Edge's free `edge-tts` endpoint, news comes from
Google News RSS, and live soccer scores come from ESPN's public JSON.

## What it does

| You say / type              | What happens                                             |
|-----------------------------|----------------------------------------------------------|
| `open spotify` / `open code`| Launches an app (desktop or web fallback)                |
| `search <query>`            | Google search in your default browser                    |
| `play <query>`              | Opens Spotify's web search                               |
| `news` / `news tech`        | Reads top headlines aloud (world, tech, sports, …)       |
| `scores`                    | Latest scores in your favorite league                    |
| `scores premier` / `scores champions` / `scores peru` | Any league on demand           |
| `weather in Vancouver`      | wttr.in one-liner (no API key)                           |
| `what time is it` / `what day is it` | Reads the clock / date                          |
| `battery` / `cpu` / `ram` / `disk` / `ip` / `wifi` | System readings via psutil        |
| `volume up` / `down` / `mute` / `set volume 40` | Master volume control          |
| `pause` / `next` / `previous`| Media keys (works with Spotify, YouTube, VLC, …)        |
| `screenshot`                | Saves a PNG to your Desktop                              |
| `set a timer for 25 minutes`| Timers with natural durations                            |
| `pomodoro`                  | 25-minute focus block, then beeps                        |
| `remind me in 30 minutes to stretch` | Voice reminders                                 |
| `timers` / `cancel timers`  | List / cancel active jobs                                |
| `joke` / `trivia`           | Random jokes (JokeAPI) and trivia (OpenTDB)              |
| `speak spanish` / `speak english` / `speak french` | Swap the neural voice     |
| `help`                      | Prints the command list                                  |
| `quit` / `exit`             | Signs off                                                |

## Install

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.toml config.toml
```

## Run

```powershell
python jarvis.py               # DEFAULT: push-to-talk (Ctrl+Alt+J from any app)
python jarvis.py --no-hotkey   # plain text loop, no hotkey
python jarvis.py --no-greet    # skip the boot greeting
python jarvis.py --voice       # one-shot microphone input
python jarvis.py --wake        # always-listening ("hey jarvis")
python jarvis.py --ui          # always-listening + floating orb HUD
```

### Push-to-talk (recommended)

Wake-word mode is unreliable in noisy rooms and burns CPU running STT
on every ambient sound. Push-to-talk is the new default:

- Press **Ctrl+Alt+J** from any application → Jarvis says "Yes?" and
  listens for one utterance.
- Speaker verification kicks in: if it's your voice → transcribe and
  dispatch. If it's someone else → silent no-op.
- Nothing is listening between presses. Zero background CPU/mic use.

Change the combo in `config.toml`:

```toml
[hotkey]
enabled = true
combo   = "ctrl+alt+j"          # or "f9", "win+space", "ctrl+shift+space", …
ack     = true                  # set false for completely silent activation
```

Startup greeting is now minimal — just **"Hello, Raul."** No date, no
time, no formality. Suppress it entirely with `--no-greet`.

Or **just double-click `launch.bat`** — it handles the venv and starts
the assistant. Four Desktop shortcuts are provided:

* **Jarvis** — text mode with morning boot
* **Jarvis Voice** — one-shot mic input per turn
* **Jarvis Always Listening** — hands-free wake-word mode
* **Jarvis HUD** — floating orb overlay, no terminal (launches `launch_ui.vbs`)

## HUD — the floating orb

`--ui` mode replaces the terminal with a **frameless, always-on-top,
translucent circular orb** docked at the right edge of your primary
screen. It reacts to Jarvis's state in real time:

| State       | Look                                                     |
|-------------|----------------------------------------------------------|
| Idle        | Dark navy orb, faint cyan halo, slow breathing pulse     |
| Listening   | Bright cyan glow + expanding sonar rings                 |
| Thinking    | Amber orb with a spinning conic-gradient loader          |
| Speaking    | Magenta orb with concentric rings pulsing on each phrase |

The orb is drag-to-move (left-click and hold), right-click for a menu
(reset position, toggle always-on-top, quit). A chip beneath the orb
shows the current state (`STANDBY` / `LISTENING` / `THINKING` /
`SPEAKING`); a smaller chip below that echoes the last thing you said
and Jarvis's reply — so you can glance at the screen instead of having
to catch every word.

Under the hood the HUD is a `QWidget` painted from scratch with
`QPainter` (radial gradients, conic gradients, rotating dashed arcs),
running on the Qt main thread. Background threads (wake listener,
voice engine) push state updates through a `JarvisBridge` `QObject`
using thread-safe queued signals — see `jarvis_ui.py` and
`ui_bridge.py`.

## Clap to launch

Even better than a wake word: **two quick claps** anywhere in the
room will launch Jarvis in wake-word mode. No terminal, no shortcut,
no keyboard — from Jarvis being closed to fully listening in about a
second.

Install the auto-start:

```powershell
powershell -ExecutionPolicy Bypass -File install_startup.ps1
```

That drops a shortcut into your Windows Startup folder pointing at
`clap_watcher.vbs`, which runs `clap_watcher.py` silently via
`pythonw.exe` (no console window). It stays running across logins.

The detector demands two amplitude spikes 150 ms - 1200 ms apart, each
above the configured threshold, with quiet in between — enough to
reject speech, TV, keyboard clatter, and door slams. A three-second
cooldown after firing prevents echo re-triggers.

Tuning knobs:

```powershell
python clap_watcher.py --threshold 0.28    # more sensitive
python clap_watcher.py --threshold 0.45    # less sensitive
python clap_watcher.py --dry-run           # detect and log, don't launch
python clap_watcher.py --list-devices      # if you have multiple mics
```

To remove the auto-start:

```powershell
powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Uninstall
```

Concurrent launches are prevented by a PID lock file
(`%TEMP%\jarvis.lock`); if Jarvis is already listening, extra claps are
ignored.

## Personality — how Jarvis addresses you

By default Jarvis speaks to you as `sir` (Iron Man style). Change it
in `config.toml`:

```toml
[user]
name    = "Raul"
address = "sir"     # try "sir", "madam", "boss", or "" to disable
```

The address is appended naturally to every spoken response — *"Timer
complete, sir."*, *"Volume set to 40 percent, sir."*, *"Signing off.
Have a productive day, sir."*

## Wake word

In wake mode Jarvis quietly listens for `hey jarvis` (or `jarvis`)
in the background. When it hears you it says `Yes?` and captures the
next thing you say as a command. You can also chain the wake and
command in one breath — *"hey jarvis, play blinding lights"* dispatches
immediately, no follow-up needed.

Customise in `config.toml`:

```toml
[wake]
enabled = false                 # or true to always launch in wake mode
phrases = ["hey jarvis", "jarvis", "computer"]
ack     = "Yes?"
```

Typed commands still work while wake mode is active — anything you
type into the terminal gets dispatched the same way as a spoken
command. While Jarvis is speaking, the listener is automatically
paused so it doesn't hear itself.

## Open / read anything by name

Jarvis can resolve **any app, file, folder or URL** on your machine by
name. Under the hood it walks a small set of roots (Desktop, Documents,
Downloads, Start Menu shortcuts, all OneDrive equivalents, plus any
extra folders you configure) and picks the best fuzzy match.

```
open spotify                   # Start Menu shortcut
open my resume                 # Raul_Resume.pdf in ~/Documents
open the downloads folder      # folder, not a file
open github.com                # URL, opens in default browser
open C:\path\to\file.pdf       # explicit path — always allowed
read the todo list             # speaks an excerpt, prints the rest
read config.toml               # reads and prints text files
find gomero                    # list matches without opening
```

`read` is guarded — it refuses non-text files and caps output at
200 KB (configurable). Nothing here writes, deletes, or executes
arbitrary shell strings; opening uses the OS default association
(same as a double-click).

Add more roots in `config.toml`:

```toml
[resolver]
extra_roots = ["C:/Users/you/projects", "D:/vault"]
read_max_bytes = 200000
```

## Recognises your voice only

Jarvis can be locked to a single speaker — yours. It uses Resemblyzer
(a small pretrained speaker-embedding model) to compare every
incoming utterance against a voiceprint you enroll once. Anyone else
speaking near your machine (family, coworkers, YouTube in the
background) is silently ignored, even if they say "hey Jarvis".

One-time setup (Windows, no C++ build tools required):

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install webrtcvad-wheels          # prebuilt binaries for Windows
pip install --no-deps resemblyzer      # skip source-only webrtcvad
pip install librosa Unidecode inflect  # remaining resemblyzer deps
python enroll_voice.py                 # records 5 short clips of your voice
```

Linux / macOS users can just `pip install torch resemblyzer` — the
`webrtcvad` source install works fine outside Windows.

Then flip the switch in `config.toml`:

```toml
[speaker]
enabled          = true
voiceprint_path  = "voiceprint.npy"
threshold        = 0.65              # 0..1. Higher = stricter.
sample_rate      = 16000
```

Notes:

- **Fail-open by default.** If `enabled=false`, or no voiceprint exists,
  or Resemblyzer isn't installed, Jarvis behaves exactly like before.
- **Silent rejection.** When a non-owner voice trips the wake word,
  Jarvis says nothing — perfect for shared spaces.
- **Tune the threshold** if it's too strict (rejects you) or too
  permissive (lets in a housemate). Around 0.55–0.75 is typical.
- **Re-enroll** any time your voice changes noticeably (cold, mic
  swap, new headset) by rerunning `enroll_voice.py`.

## Natural language (LLM fallback)

Out of the box Jarvis matches commands with a regex table. That's fast
and offline, but it means you have to phrase things a specific way.

Turn on the LLM fallback and Jarvis handles free-form requests too:

- *"pon música chill en spotify"* → `play chill music`
- *"sube el volumen un poquito"* → `volume up`
- *"qué hora es y cuánto falta para las 5?"* → tells the time and does the math
- *"cómo se dice hello en francés?"* → answers in French out loud
- *"resume in one sentence what a raspberry pi is"* → short spoken answer

When nothing in the built-in intent table matches, the transcript is
sent to Gemini with a system prompt describing every canonical command
Jarvis knows. Gemini either emits a canonical command (re-dispatched
through the same code path as a typed one) or a short conversational
reply that Jarvis speaks back. The last few exchanges are kept as
context so follow-ups like *"and tomorrow?"* still work.

Enable it in `config.toml`:

```toml
[llm]
enabled         = true
api_key         = "..."             # or export GEMINI_API_KEY instead
model           = "gemini-3.6-flash"
temperature     = 0.3
timeout_seconds = 6.0
history_size    = 5
```

Free API key: <https://aistudio.google.com/app/apikey>. No new pip
package — the module uses `requests`, which was already required for
news and soccer.

## Voice

The default is `en-US-JennyNeural` (warm female US). Change it in
`config.toml`:

```toml
[voice]
engine     = "auto"                # "auto" | "edge" | "sapi"
voice_name = "en-US-AriaNeural"    # neural voice for edge-tts
rate       = 180
volume     = 0.9
```

Popular neural voices:

| Name                    | Style                                |
|-------------------------|--------------------------------------|
| `en-US-JennyNeural`     | Warm female US (default)             |
| `en-US-AriaNeural`      | Crisp female US, news-anchor         |
| `en-US-MichelleNeural`  | Younger female US                    |
| `en-GB-SoniaNeural`     | Female UK                            |
| `es-ES-ElviraNeural`    | Female Spain                         |
| `es-MX-DaliaNeural`     | Female Mexico                        |
| `fr-FR-DeniseNeural`    | Female France                        |

Full list: `edge-tts --list-voices` (over 400 available).

If `edge-tts` fails or is disabled, Jarvis falls back to Windows SAPI5
(picks the first female voice it finds — usually Zira).

## News

Uses Google News RSS. Available topics: `world`, `tech`, `sports`,
`business`, `science`, `health`, `entertainment`. Anything else is
treated as a search phrase:

```
news                          → top world headlines
news tech                     → technology section
news about semiconductors     → free-form search
what's happening in ai        → same, alternate phrasing
```

## Soccer

Live scores + fixtures + finals via ESPN. Recognized league keywords:

`la liga`, `premier` / `epl`, `champions` / `ucl`, `serie a`,
`bundesliga`, `ligue 1`, `mls`, `peru` / `liga 1`, `libertadores`,
`europa`, `world cup`.

```
scores                → your favorite league (from config)
scores champions      → today's UCL fixtures / results
scores peru           → Liga 1 Peruana
who's playing today   → same as scores
```

Set your default in `config.toml`:

```toml
[soccer]
favorite_league = "la liga"
```

## Add a new skill

Drop one line into `INTENTS` in `commands.py`:

```python
(r"^lock (my|the) (pc|screen)$",
 lambda d, m: d._open_app("terminal")),   # or write your own handler
```

That's it. Regex + callable. No framework, no boilerplate.

## Files

```
python-tools/jarvis/
├── jarvis.py            entry point + boot sequence
├── commands.py          intent table + dispatcher
├── voice.py             edge-tts (neural) → SAPI5 fallback
├── news.py              Google News RSS
├── soccer.py            ESPN scoreboard JSON
├── system.py            battery, cpu, ram, disk, ip, wifi, volume, media keys, screenshot
├── fun.py               jokes (JokeAPI) + trivia (OpenTDB)
├── scheduler.py         timers, pomodoros, reminders (threaded)
├── wake.py              background wake-word listener ("hey jarvis")
├── clap_watcher.py      standalone service: 2 claps -> launch Jarvis
├── clap_watcher.vbs     silent (no console) launcher for the watcher
├── install_startup.ps1  add/remove Windows Startup shortcut for clap watcher
├── launch.bat           one-click launcher for the Desktop shortcut
├── config.example.toml
├── requirements.txt
├── LICENSE (MIT)
└── README.md
```
