# Jarvis

A tiny personal assistant I run in the morning. Boots with a spoken
greeting, opens Spotify + Google, then waits for typed (or spoken)
commands: `open code`, `play weeknd blinding lights`, `search rust cli
crates`, `weather in Vancouver`, `what time is it`, `quit`.

It's a small, honest Jarvis — offline TTS via pyttsx3, no cloud
dependencies for the core, and a regex-based intent table you can extend
with one line per skill.

## Install

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.toml config.toml
```

If you want microphone input, on Windows also run:

```powershell
pip install pipwin
pipwin install pyaudio
```

## Run

```powershell
# boot routine + text loop
python jarvis.py

# skip the "good morning" boot
python jarvis.py --no-greet

# talk to it through the microphone
python jarvis.py --voice
```

## Commands

| You say / type              | What happens                                             |
|-----------------------------|----------------------------------------------------------|
| `open spotify`              | Launches Spotify (desktop or web fallback)               |
| `open code`                 | Opens VS Code                                            |
| `open <anything mapped>`    | Runs whatever the config maps that name to               |
| `search rust cli crates`    | Google search in your default browser                    |
| `play <query>`              | Opens Spotify's web search for the query                 |
| `weather in Vancouver`      | wttr.in short summary (no API key needed)                |
| `what time is it`           | Reads the clock                                          |
| `what day is it`            | Reads today's date                                       |
| `help`                      | Prints this command list                                 |
| `quit` / `exit`             | Signs off and exits                                      |

## Add a new skill

Open `commands.py` and add one line to `INTENTS`:

```python
(r"^lock (my|the) (pc|screen)$",
 lambda d, m: d._open_app("terminal") ),  # or write your own handler
```

That's it. No boilerplate, no framework, one regex + one callable.

## Config

`config.toml` (copied from `config.example.toml`):

* `user.name` — used in the morning greeting
* `voice.rate`, `voice.volume` — TTS tuning
* `startup.open_spotify`, `startup.open_google`, `startup.extra_urls` —
  what boots with the assistant
* `apps.<name>` — friendly aliases for launchable programs

## Files

```
python-tools/jarvis/
├── jarvis.py            main entry point + boot sequence
├── commands.py          intent table + dispatcher
├── voice.py             pyttsx3 wrapper with graceful degradation
├── config.example.toml
├── requirements.txt
├── LICENSE (MIT)
└── README.md
```
