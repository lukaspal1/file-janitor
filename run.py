#!/usr/bin/env python3
"""Entry point for File Janitor."""
import sys
from PyQt6.QtWidgets import QApplication

from janitor.ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("File Janitor")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())