Output demo.gif
Set Shell "zsh"
Set FontSize 18
Set Width 1800
Set Height 1000
Set TypingSpeed 0.05

# Start demo: launch dashboard with socket bridge enabled for mock feed.
Type "printf 'Starting dashboard demo...\\n'" Enter
Type "python -m gratekeeper.textual_dashboard --socket /tmp/gratekeeper.sock --fetch 5 --refresh 1" Enter
Sleep 2

# Overlay: change theme
Sleep 900ms
Type "t"
Sleep 1
Type "t"
Sleep 1

# Overlay: Active-only
Sleep 900ms
Type "a"
Sleep 1

# Overlay: Manual & digits
Sleep 1100ms
Type "m"
Sleep 1
Type "1"
Sleep 1
Type "1"
Sleep 1

# Overlay: Hide Actions
Sleep 900ms
Type "x"
Sleep 1

# Overlay: Help/keys and dismiss
Sleep 1100ms
Type "?"
Sleep 1
Escape
Sleep 1

# End
Sleep 900ms
Sleep 1

# Gracefully end the dashboard process so recording closes cleanly
Ctrl+C
Sleep 500ms
