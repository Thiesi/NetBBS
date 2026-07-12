"""
Static assets for the web transport (design doc round 22/25):
`static/index.html`, vendored `xterm.js`/`xterm.css`/the fit addon
(MIT-licensed, see `static/xterm-LICENSE.txt`), and this project's own
`static/netbbs-terminal.js` shim. No Python logic lives here — the
actual `WebSession`/`WebServer` implementation is
`netbbs.net.web`, alongside `netbbs.net.telnet`/`netbbs.net.ssh`.
Kept as a separate top-level package specifically for the static
files, per design doc round 22 point 8.
"""
