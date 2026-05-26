import sys
import subprocess


def pager(text):
    if not text or not sys.stdout.isatty():
        print(text)
        return
    try:
        p = subprocess.Popen(["less", "-R", "-F", "-X"], stdin=subprocess.PIPE, text=True)
        p.communicate(text)
    except FileNotFoundError:
        print(text)
