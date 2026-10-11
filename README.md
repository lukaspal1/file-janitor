# File Janitor

A desktop app (Python + PyQt6) that tidies up your files. It scans folders, proposes where each file should go, and only moves anything after you approve it.

## Features

- **Rule-based sorting.** Files are matched by extension, name, or path using `rules.toml`. Subfolders you made are preserved, so `Desktop/cabinets/a.jpg` goes to `Pictures/Sorted/cabinets/a.jpg`.
- **Sort by category.** Type categories like "Books" or "Tool catalogues". A local AI model reads each document and decides where it fits. Nothing leaves your PC.
- **"Belongs here" check.** After every scan, files that look like they belong where they are get unticked and listed under *Suggested to keep*, with the reasons. Examples are files that are part of a code project or IDE workspace, inside a program or game data folder, used by a nearby file (like an image linked from a page), or (with the local AI) documents whose content matches their folder.
- **Game folders into My Games.** When a scan covers Documents, game data folders (Battlefield, Call of Duty, …, or any folder with saves inside) are offered as whole-folder moves into `Documents/My Games`. Only folders untouched for a year are ticked, because a game you still play would recreate its folder and lose its settings.
- **Desktop cleanup.** Moves whole Desktop folders and tidies loose files, and can arrange the desktop icons into groups (Windows).
- **Duplicate finder.** Finds identical files and sends the extras to the Recycle Bin.
- **Safe by default.** It skips system and app folders, code projects, and cloud-synced files. Every action is logged and can be undone.

## Setup

```
pip install -r requirements.txt
python run.py
```

Settings are in `config.toml`, rules in `rules.toml`, and categories in `categories.toml`.
