"""
Static assets for the web transport (design doc):
`static/index.html`, vendored `xterm.js`/`xterm.css` (6.0.0) and the
fit and WebGL addons (`@xterm/addon-webgl` 0.19.0, for box-drawing
glyphs that join between rows, issue #1083), all MIT-licensed by the
xterm.js authors (see `static/xterm-LICENSE.txt`), and this project's own
`static/netbbs-terminal.js` shim. No Python logic lives here — the
actual `WebSession`/`WebServer` implementation is
`netbbs.net.web`, alongside `netbbs.net.telnet`/`netbbs.net.ssh`.
Kept as a separate top-level package specifically for the static
files, per the design doc.
"""
