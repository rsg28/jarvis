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
python jarvis.py               # boot routine + text loop
python jarvis.py --no-greet    # skip the "good morning"
python jarvis.py --voice       # talk to it through the microphone
```

Or **just double-click `launch.bat`** — it handles the venv and starts
the assistant. Two Desktop shortcuts are provided (`Jarvis`, `Jarvis
Voice`) if you drop them there.

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
├── launch.bat           one-click launcher for the Desktop shortcut
├── config.example.toml
├── requirements.txt
├── LICENSE (MIT)
└── README.md
```
