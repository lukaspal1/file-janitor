"""
Global configuration, defaults, and platform detection.

Anything the user might want to change lives in config.toml.
This file holds constants that shouldn't change per-user.
"""
from __future__ import annotations

import os
import sys
import platform
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# ---- Platform ----
IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# ---- App paths ----
APP_DIR = Path.home() / ".file_janitor"
APP_DIR.mkdir(exist_ok=True)
DB_PATH = APP_DIR / "janitor.db"
LOG_PATH = APP_DIR / "janitor.log"

# Config + rules live next to the app for now; can move to APP_DIR later
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_RULES_PATH = PROJECT_ROOT / "rules.toml"

# ---- System directories that are NEVER scanned ----
# These are "already sorted" per the user's requirement.
SYSTEM_PATHS_WINDOWS = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
    r"C:\Recovery",
    r"C:\PerfLogs",
]
SYSTEM_PATHS_MAC = [
    "/System", "/Library", "/private", "/usr", "/bin", "/sbin",
    "/etc", "/var", "/opt", "/cores", "/Volumes",
]
SYSTEM_PATHS_LINUX = [
    "/proc", "/sys", "/dev", "/run", "/boot", "/usr", "/bin",
    "/sbin", "/etc", "/var", "/lib", "/lib64", "/opt", "/srv", "/root",
]

def system_paths() -> list[Path]:
    if IS_WINDOWS:
        return [Path(p) for p in SYSTEM_PATHS_WINDOWS]
    if IS_MAC:
        return [Path(p) for p in SYSTEM_PATHS_MAC]
    if IS_LINUX:
        return [Path(p) for p in SYSTEM_PATHS_LINUX]
    return []

# ---- Per-user app/program folders under the home directory ----
# These hold installed programs (e.g. AppData\Local\Programs\Python) and
# app state. Moving anything out of them breaks software.
HOME_SKIP_DIRS = [
    "AppData", "Application Data", "Local Settings",   # Windows
    "Library", "Applications",                          # macOS
    "snap",                                             # Linux
]

def home_skip_paths() -> list[Path]:
    home = Path.home()
    return [home / name for name in HOME_SKIP_DIRS]

# ---- "Container" folders ----
# Generic places where people drop things. When a file is moved out of a
# subfolder of one of these, the subfolder path is kept at the destination:
#   ~/Desktop/cabinets/a.jpg  ->  ~/Pictures/Sorted/cabinets/a.jpg
CONTAINER_DIR_NAMES = [
    "Desktop", "Downloads", "Documents", "Pictures", "Videos", "Music",
    "Movies",
]

# ---- Directories to skip by name, anywhere on disk ----
# These are dev artifacts, caches, version control, virtualenvs.
SKIP_DIR_NAMES = {
    # VCS
    ".git", ".svn", ".hg",
    # Python
    "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "site-packages", ".eggs",
    # Node
    "node_modules", ".next", ".nuxt", ".parcel-cache", ".turbo",
    # Build outputs
    "dist", "build", "target", "out", ".gradle", ".idea", ".vs",
    # Caches / system
    ".cache", ".Trash", ".Trash-1000", "$RECYCLE.BIN",
    "System Volume Information",
    # Cloud sync internals
    ".dropbox", ".dropbox.cache",
    # Mac
    ".DS_Store", ".Spotlight-V100", ".fseventsd",
}

# ---- Cloud sync roots (never move files inside these) ----
CLOUD_SYNC_DIR_NAMES = {
    "OneDrive", "OneDrive - Personal", "OneDrive - Business",
    "Dropbox", "Google Drive", "GoogleDrive", "iCloud Drive",
    "Box", "Box Sync", "pCloud", "MEGA", "Sync",
}

# ---- File category extensions ----
# Used by the default rules in rules.toml, but the rules file is the
# source of truth. These are just here so the UI can show category icons.
EXT_VIDEO = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp"}
EXT_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".svg", ".heic", ".raw", ".cr2", ".nef"}
EXT_AUDIO = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"}
EXT_DOCUMENT = {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".tex"}
EXT_SPREADSHEET = {".xls", ".xlsx", ".csv", ".ods", ".tsv"}
EXT_PRESENTATION = {".ppt", ".pptx", ".odp", ".key"}
EXT_ARCHIVE = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"}
EXT_CODE = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh",
    ".bash", ".zsh", ".ps1", ".bat", ".html", ".css", ".scss", ".sass",
    ".sql", ".r", ".m", ".lua", ".pl", ".ex", ".exs", ".clj", ".hs",
    ".vue", ".svelte", ".dart", ".zig", ".nim", ".jl",
}

# ---- Project markers ----
# If any of these exist in a directory (or its ancestors up to N levels),
# the directory is considered a "project" and its code files are left alone.
PROJECT_MARKERS = {
    ".git", ".hg", ".svn",
    "package.json", "pyproject.toml", "setup.py", "requirements.txt",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "*.sln", "*.csproj", "*.fsproj", "*.vbproj",
    "Gemfile", "composer.json", "mix.exs", "Project.toml",
    "Makefile", "CMakeLists.txt", "Dockerfile", "docker-compose.yml",
    # IDE / workspace files
    ".project", ".classpath", ".idea", "*.iml", "*.code-workspace",
    "*.uproject", "*.Rproj", "*.xcodeproj", "*.sublime-project",
    "Doxyfile", "*.doxyfile",
}
# How many parent directories to check for a project marker
PROJECT_MARKER_MAX_DEPTH = 4

# ---- App / game data folders ----
# Folders that programs create in Documents (or home) for settings, saves,
# presets and plugins. Matched case-insensitively against the folder name;
# an entry ending in "*" matches as a prefix.
GAME_DATA_DIR_NAMES = {
    "Rockstar Games", "EA Games", "Electronic Arts", "Ubisoft", "Klei",
    "Paradox Interactive", "Respawn", "Riot Games", "League of Legends",
    "Battlefield*", "Call of Duty*", "The Witcher*", "Guild Wars*",
    "Elder Scrolls*", "Euro Truck Simulator*", "American Truck Simulator",
    "Dolphin Emulator", "Assassin's Creed*", "Far Cry*", "Grand Theft Auto*",
    "BioWare", "Larian Studios", "CD Projekt Red", "FromSoftware", "NBA 2K*",
    "FIFA*", "EA SPORTS*", "Apex Legends", "Overwatch", "Rocket League",
    "Halo*", "Destiny*", "Diablo*", "StarCraft*", "Warcraft*", "Hearthstone",
    "Cyberpunk*", "Red Dead*", "Watch Dogs*", "Rainbow Six*", "Anno *",
    "Football Manager*", "Need for Speed*", "Mass Effect*", "Dragon Age*",
    "Dark Souls*", "Elden Ring", "Sekiro", "Terraria", "Stardew Valley",
    "Hogwarts Legacy", "Starfield", "XDefiant",
}
APP_DATA_DIR_NAMES = GAME_DATA_DIR_NAMES | {
    "My Games", "Saved Games", "Image-Line", "Blizzard", "Battle.net", "Steam",
    "Native Instruments", "Ableton", "Arduino",
    "WindowsPowerShell", "PowerShell", "Custom Office Templates",
    "OneNote Notebooks", "Visual Studio*", "SQL Server Management Studio*",
}
# A Documents folder with one of these inside is treated as a game's data.
GAME_HINT_DIRS = {"saves", "savegames", "savegame", "savedata", "players", "replays"}
# Game folders in Documents can be gathered into Documents/My Games. Only
# ones untouched this long are ticked: a game still in use would recreate
# its folder in Documents and lose its settings.
GAMES_DIR_NAME = "My Games"
GAME_FOLDER_STALE_DAYS = 365
# A folder holding one of these subfolders is treated as app/game data.
APP_DATA_HINT_DIRS = {
    "settings", "saves", "savegames", "save", "savedata", "profiles",
    "players", "config", "configs", "cfg", "mods", "plugins", "presets",
}

# ---- Game folder heuristic ----
# A folder is "probably a game" if it contains any of these AND
# any executable/asset signature. We err on the side of NOT touching.
GAME_MARKER_FILES = {"steam_appid.txt", "steam_api.dll", "steam_api64.dll", "unityplayer.dll", "*.exe"}
GAME_MARKER_DIRS = {"Binaries", "Content", "Engine", "Saved", "steamapps", "Unity", "unity"}

# ---- Safety thresholds ----
# Cross-drive moves are slow; warn above this many bytes in one batch.
CROSS_DRIVE_WARN_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB
# If a scan proposes more than this many moves, force a confirmation dialog.
LARGE_BATCH_WARN_COUNT = 100

# ---- Logging ----
LOG_LEVEL = os.environ.get("JANITOR_LOG", "INFO").upper()


# ---- User configuration (config.toml) ----

@dataclass
class AppConfig:
    roots: list[Path] = field(default_factory=list)
    exclude_paths: list[Path] = field(default_factory=list)
    skip_cloud_sync: bool = True
    follow_symlinks: bool = False
    min_size: int = 0
    max_depth: int = 0
    confirm_cross_drive: bool = True
    name_conflict: str = "rename"
    preserve_folders: bool = True
    keep_check_ai: bool = True


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Read config.toml. Missing file or keys fall back to defaults."""
    if not path.exists():
        return AppConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    scan = data.get("scan", {})
    move = data.get("move", {})
    name_conflict = move.get("name_conflict", "rename")
    if name_conflict not in ("rename", "skip"):
        raise ValueError(f"name_conflict must be 'rename' or 'skip', got {name_conflict!r}")

    return AppConfig(
        roots=[Path(p).expanduser() for p in scan.get("roots", [])],
        exclude_paths=[Path(p).expanduser() for p in scan.get("exclude_paths", [])],
        skip_cloud_sync=bool(scan.get("skip_cloud_sync", True)),
        follow_symlinks=bool(scan.get("follow_symlinks", False)),
        min_size=int(scan.get("min_size", 0)),
        max_depth=int(scan.get("max_depth", 0)),
        confirm_cross_drive=bool(move.get("confirm_cross_drive", True)),
        name_conflict=name_conflict,
        preserve_folders=bool(move.get("preserve_folders", True)),
        keep_check_ai=bool(scan.get("keep_check_ai", True)),
    )