"""
main.py
=======
Entry point for the Balfund Simple Algo Platform (Zerodha Kite).
"""

import multiprocessing


def main():
    multiprocessing.freeze_support()   # safe for PyInstaller --onefile
    from gui import run
    run()


if __name__ == "__main__":
    main()
