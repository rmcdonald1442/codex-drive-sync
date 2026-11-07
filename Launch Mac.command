#!/bin/zsh
cd -- "${0:A:h}" || exit 1
export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if command -v python3.14 >/dev/null 2>&1; then
    python3.14 app.py
else
    python3 app.py
fi
result=$?
if (( result != 0 )); then
    print "Codex Drive Sync needs Python 3.14 or newer with Tk."
    read "reply?Press Return to close. "
fi
exit "$result"
