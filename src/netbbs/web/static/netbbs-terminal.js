// NetBBS web terminal shim (design doc round 22/25).
//
// Speaks the structured JSON protocol netbbs.net.web.WebSession expects:
//   browser -> server: {"type": "key", "data": "<raw onData string>"}
//                       {"type": "resize", "cols": N, "rows": N}
//   server -> browser: {"type": "output", "data": "<text to display>"}
// Door mode adds stream-tagged door_key/door_output frames; output carries
// base64 UTF-8 bytes (legacy CP437 is converted by the server). A streaming
// decoder preserves code points split across frames.
//
// Menus deliberately do not use raw byte passthrough (addon-attach) --
// see design doc round 22 point 7 for why: a browser has already
// resolved the raw-terminal-byte ambiguity a byte-oriented protocol
// exists to handle, and structured messages give resize a first-class
// signal instead of a bolted-on side channel.
(function () {
  "use strict";

  var term = new Terminal({
    cursorBlink: true,
    scrollback: 2000,
    fontFamily: '"Cascadia Code", "Fira Code", "JetBrains Mono", "Consolas", "Courier New", monospace',
    fontSize: 15,
    letterSpacing: 0,
    lineHeight: 1.15,
    theme: {
      background: "#0c0d10",
      foreground: "#e2e8f0",
      cursor: "#f6ad55",
      cursorAccent: "#000000",
      selectionBackground: "#4a5568",
    },
  });
  var fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById("terminal"));
  fitAddon.fit();

  var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  var ws = new WebSocket(scheme + "//" + window.location.host + "/ws");
  var doorStream = null;
  var doorDecoder = null;
  var fixedDoorSize = false;

  function sendResize() {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }
  }

  ws.onopen = function () {
    sendResize();
  };

  ws.onmessage = function (event) {
    var msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    if (msg.type === "door_mode") {
      if (doorDecoder) term.write(doorDecoder.decode());
      doorStream = msg.active ? msg.stream : null;
      doorDecoder = msg.active ? new TextDecoder("utf-8") : null;
      fixedDoorSize = !!(msg.active && msg.cols && msg.rows);
      if (fixedDoorSize) term.resize(msg.cols, msg.rows);
      else fitAddon.fit();
      if (!msg.active) sendResize();
    } else if (msg.type === "door_output" && msg.stream === doorStream && doorDecoder) {
      var bytes = Uint8Array.from(atob(msg.data), function (c) { return c.charCodeAt(0); });
      term.write(doorDecoder.decode(bytes, { stream: true }));
    } else if (msg.type === "output" && typeof msg.data === "string") {
      term.write(msg.data);
    } else if (msg.type === "transfer" && typeof msg.url === "string") {
      // Issue #475: the BBS has handed this browser a one-use transfer
      // link. A download starts by itself; an upload opens a drop
      // target, because the caller is already in a browser and should
      // not have to copy a URL out of a terminal.
      if (msg.direction === "download") startDownload(msg.url, msg.filename);
      else openUploadPanel(msg.url);
    }
  };

  ws.onclose = function () {
    term.write("\r\n\x1b[90m[Connection closed]\x1b[0m\r\n");
  };

  ws.onerror = function () {
    term.write("\r\n\x1b[90m[Connection error]\x1b[0m\r\n");
  };

  term.onData(function (data) {
    if (ws.readyState === WebSocket.OPEN) {
      // Bound pasted chunks and browser-side queued writes as well as server queues.
      var chars = Array.from(data);
      for (var i = 0; i < chars.length; i += 1024) {
        if (ws.bufferedAmount > 65536) { ws.close(); return; }
        ws.send(JSON.stringify({ type: doorStream === null ? "key" : "door_key",
                                stream: doorStream, data: chars.slice(i, i + 1024).join("") }));
      }
    }
  });


  // -- file transfer (issue #475) ---------------------------------------
  //
  // Zmodem cannot work in a browser tab, so the BBS hands this page a
  // single-use HTTP link instead. Everything below is presentation: the
  // link is already scoped to one caller, one file or area, and one use
  // by the server, and nothing here can widen it.

  function startDownload(url, filename) {
    // An anchor click rather than assigning window.location, so the tab
    // keeps the live terminal session rather than navigating away from
    // it mid-transfer.
    var link = document.createElement("a");
    link.href = url;
    if (filename) link.download = filename;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  function openUploadPanel(url) {
    var existing = document.getElementById("transfer-panel");
    if (existing) existing.remove();

    var panel = document.createElement("div");
    panel.id = "transfer-panel";
    panel.className = "transfer-panel";
    panel.innerHTML =
      '<div class="transfer-card">' +
      '<h2>Upload a file</h2>' +
      '<p class="transfer-drop" id="transfer-drop">Drop a file here, or choose one.</p>' +
      '<p><input type="file" id="transfer-input"></p>' +
      '<p class="transfer-status" id="transfer-status">This link works once.</p>' +
      '<p><button type="button" id="transfer-cancel">Cancel</button></p>' +
      "</div>";
    document.body.appendChild(panel);

    var drop = panel.querySelector("#transfer-drop");
    var input = panel.querySelector("#transfer-input");
    var status = panel.querySelector("#transfer-status");
    var done = false;
    var inFlight = null;

    function close() {
      // Cancel means cancel (issue #475 review): without this the panel
      // disappears while the browser keeps sending the file, and the
      // caller believes they stopped it.
      if (inFlight) inFlight.abort();
      panel.remove();
      term.focus();
    }

    function send(file) {
      if (done || !file) return;
      done = true;
      status.textContent = "Uploading " + file.name + "...";
      var body = new FormData();
      body.append("file", file, file.name);
      inFlight = typeof AbortController === "function" ? new AbortController() : null;
      fetch(url, { method: "POST", body: body, signal: inFlight ? inFlight.signal : undefined })
        .then(function (response) {
          if (!response.ok) return response.text().then(function (text) { throw new Error(text); });
          return response.json();
        })
        .then(function (stored) {
          status.textContent = "Uploaded " + stored.filename + ".";
          // Nudge the BBS into repainting, so the file appears in the
          // listing the caller is looking at rather than after their
          // next keystroke.
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "key", data: "\f" }));
          }
          setTimeout(close, 1200);
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") return;  // the caller cancelled
          done = false;
          inFlight = null;
          status.textContent = "Upload failed: " + (error && error.message ? error.message : error);
        });
    }

    input.addEventListener("change", function () { send(input.files && input.files[0]); });
    panel.querySelector("#transfer-cancel").addEventListener("click", close);
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.classList.remove("is-over");
      });
    });
    drop.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      send(files && files[0]);
    });
    input.focus();
  }

  window.addEventListener("resize", function () {
    if (!fixedDoorSize) fitAddon.fit();
    sendResize();
  });
})();
