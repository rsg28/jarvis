"""
GameWatcher — auto-release VRAM when a game launches.

A background daemon that polls the process list every N seconds and
decides "is a game running right now?" using two signals:

  1. Executable name is in `game_processes` (exact match, case-insensitive)
  2. Executable path contains any `game_path_hints` substring
     (e.g. "steamapps/common", "epic games/", "riot games/")

Both signals are explicit — no fuzzy guessing — so Chrome doesn't get
mistaken for a game just because it uses the GPU.

State machine:
    not_running --[game appears]--> running   : on_game_start()
    running     --[game disappears]--> not_running : on_game_stop()

Callbacks run on the daemon thread. The caller is responsible for any
UI-thread marshalling (we don't touch tkinter / voice directly here).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Set


# Default list of common game executables so the user doesn't have to
# enumerate everything up front. Extend via [gaming].game_processes in
# config.toml. Launcher processes (steam.exe, epicgameslauncher.exe,
# etc.) are intentionally NOT here — launchers alone shouldn't trigger
# a VRAM release, only the game process itself.
DEFAULT_GAME_PROCESSES = {
    # Shooters
    "r5apex.exe", "r5apex_dx12.exe",                        # Apex
    "valorant.exe", "valorant-win64-shipping.exe",
    "cs2.exe", "csgo.exe",
    "overwatch.exe",
    "destiny2.exe",
    "callofduty.exe", "cod.exe", "modernwarfare.exe",
    "pubg.exe", "tslgame.exe",
    "fortniteclient-win64-shipping.exe",
    # RPG / adventure
    "cyberpunk2077.exe",
    "witcher3.exe",
    "eldenring.exe",
    "re4.exe", "re2.exe", "re3.exe", "re8.exe",
    "ds3.exe", "darksouls.exe", "darksoulsiii.exe",
    "sekiro.exe",
    "baldursgate3.exe", "bg3.exe", "bg3_dx11.exe",
    "hogwartslegacy.exe",
    "starfield.exe",
    "skyrimse.exe", "skyrim.exe",
    "falloutnv.exe", "fallout4.exe",
    # Multiplayer / MOBA
    "leagueclient.exe", "league of legends.exe",
    "dota2.exe",
    "minecraft.exe", "javaw.exe",                           # javaw catches modded MC
    # Racing / sims
    "f1_24.exe", "f1_23.exe", "f1_22.exe",
    "forzahorizon5.exe", "forzahorizon4.exe",
    # Marvel Rivals (several possible process names on win64 builds)
    "marvel-win64-shipping.exe",
    "marvelrivals.exe",
    "marvelrivals_launcher.exe",
    # Raul's current Steam library (scanned 2026-10-08). The path
    # hint `steamapps/common` catches these too, but having explicit
    # names makes the detection O(1) and self-documenting.
    "spider-man2.exe",                     # Marvel's Spider-Man 2
    "mtfssteam-win64-shipping.exe",        # MARVEL Tokon: Fighting Souls
    "symulatorit-win64-shipping.exe",      # ServiceIT: You can do IT
    "overcooked2.exe",                     # Overcooked! 2
    "pc creator.exe",                      # PC Building Simulator
    "among us.exe",                        # Among Us
    "how to fish.exe",                     # How to Fish
    "tftclient-win64-shipping.exe",        # Teamfight Tactics
    "phasmophobia.exe", "phasmo-win64-shipping.exe",  # Phasmophobia (both variants)
    "warframe.x64.exe", "warframe.exe",    # Warframe
    "arkascended.exe", "shootergame.exe",  # ARK (Ascended + original UE4)
    "re4.exe",                             # RE4 Remake Chainsaw Demo
    "bombanana.exe",                       # BOMBANANA Demo
    "abinfinite.exe",                      # ABInfinite
}

DEFAULT_PATH_HINTS = [
    r"\steamapps\common\\",
    r"\epic games\\",
    r"\riot games\\",
    r"\ea games\\",
    r"\ubisoft\\",
    r"\gog games\\",
    r"\battle.net\\",
    r"\xboxgames\\",
    r"\rockstar games\\",
]


@dataclass
class GameWatchState:
    running: bool = False
    poll_s: float = 10.0
    game_processes: Set[str] = field(default_factory=set)
    path_hints: List[str] = field(default_factory=list)
    auto_reclaim_on_exit: bool = True
    # Current detected game (exe name) when a game is up, else None.
    active_game: Optional[str] = None
    # Diagnostics
    checks: int = 0
    detections: int = 0
    last_change_ts: float = 0.0


class GameWatcher:
    def __init__(self,
                 on_game_start: Callable[[str], None],
                 on_game_stop: Callable[[str], None],
                 poll_s: float = 10.0,
                 extra_processes: Optional[Iterable[str]] = None,
                 extra_path_hints: Optional[Iterable[str]] = None,
                 auto_reclaim_on_exit: bool = True) -> None:
        self._on_start = on_game_start
        self._on_stop  = on_game_stop
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        procs = {p.lower() for p in DEFAULT_GAME_PROCESSES}
        if extra_processes:
            procs |= {p.lower() for p in extra_processes}
        hints = list(DEFAULT_PATH_HINTS)
        if extra_path_hints:
            hints += [h.lower() for h in extra_path_hints]
        self.state = GameWatchState(
            poll_s=max(2.0, float(poll_s)),
            game_processes=procs,
            path_hints=hints,
            auto_reclaim_on_exit=auto_reclaim_on_exit,
        )

    # ────────────── public ──────────────
    def start(self) -> bool:
        if self.state.running:
            return False
        self._stop_evt.clear()
        self.state.running = True
        self._thread = threading.Thread(
            target=self._loop, name="jarvis-gpu-watch", daemon=True)
        self._thread.start()
        logging.info("[gpu-watch] started (poll=%.1fs, %d known game "
                     "exes, %d path hints)",
                     self.state.poll_s,
                     len(self.state.game_processes),
                     len(self.state.path_hints))
        return True

    def stop(self) -> bool:
        if not self.state.running:
            return False
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.state.running = False
        logging.info("[gpu-watch] stopped")
        return True

    # ────────────── internals ──────────────
    def _detect_game(self) -> Optional[str]:
        """Return the exe name of a currently-running game, else None.
        First match wins — we only need to know THAT a game is up."""
        try:
            import psutil
        except ImportError:
            return None
        for proc in psutil.process_iter(attrs=["name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name in self.state.game_processes:
                    return name
                exe = (proc.info.get("exe") or "").lower()
                if exe and any(h in exe for h in self.state.path_hints):
                    return name or exe
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                continue
        return None

    def _loop(self) -> None:
        logging.info("[gpu-watch] loop running")
        while not self._stop_evt.is_set():
            try:
                found = self._detect_game()
                self.state.checks += 1
                if found and not self.state.active_game:
                    # Game just appeared
                    self.state.active_game = found
                    self.state.detections += 1
                    self.state.last_change_ts = time.time()
                    try:
                        self._on_start(found)
                    except Exception as exc:
                        logging.warning("[gpu-watch] on_start raised: %s", exc)
                elif not found and self.state.active_game:
                    # Game just closed
                    gone = self.state.active_game
                    self.state.active_game = None
                    self.state.last_change_ts = time.time()
                    try:
                        self._on_stop(gone)
                    except Exception as exc:
                        logging.warning("[gpu-watch] on_stop raised: %s", exc)
            except Exception as exc:
                logging.warning("[gpu-watch] poll failed: %s", exc)
            # Sleep in small slices so stop() returns quickly
            waited = 0.0
            step = 0.5
            while waited < self.state.poll_s and not self._stop_evt.is_set():
                time.sleep(step)
                waited += step
