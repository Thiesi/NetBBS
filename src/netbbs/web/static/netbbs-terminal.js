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

  function transferUrl(url) {
    // The link the BBS hands over is built from `[web] public_url`, which
    // need not be the origin this page was reached on (issue #511): a
    // cross-origin fetch cannot read the response, and the endpoint sends
    // no CORS headers. Resolving the token against this page's own address
    // is same-origin by construction and keeps whatever path prefix a
    // reverse proxy serves the page under (`/bbs/` -> `/bbs/transfer/...`).
    try {
      var token = new URL(url).pathname.split("/").pop();
      // The page's own path as a directory: a proxy can serve it at
      // `/bbs` as well as `/bbs/`, and resolving against `/bbs` would drop
      // the prefix (Codex review of #702). A last segment with a dot in it
      // (`/index.html`) is a file and is left alone.
      var page = new URL(window.location.href);
      var last = page.pathname.split("/").pop();
      if (last && last.indexOf(".") === -1) page.pathname += "/";
      return new URL("transfer/" + token, page).href;
    } catch (error) {
      return url;
    }
  }

  function startDownload(url, filename) {
    var target = transferUrl(url);
    if (typeof fetch !== "function") {
      saveDownload(target, filename);
      return;
    }
    // Probe first (issue #511): an `<a download>` saves whatever comes
    // back under the requested name, so a refused download would land as
    // a file that looks like the one asked for. HEAD spends nothing and
    // answers as the GET would; only a yes starts the real download, which
    // the anchor then streams to disk rather than this page buffering it.
    fetch(target, { method: "HEAD", cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (response.ok) {
          saveDownload(target, filename);
          return;
        }
        var reason = response.headers.get("X-NetBBS-Transfer-Message") ||
          ("The BBS refused the download (HTTP " + response.status + ").");
        showTransferNotice("Download failed", reason,
          response.status === 429 ? function () { startDownload(url, filename); } : null);
      })
      .catch(function () {
        showTransferNotice("Download failed",
          "Could not reach the BBS to start the download. Ask it for a new link.", null);
      });
  }

  function saveDownload(target, filename) {
    // An anchor click rather than assigning window.location, so the tab
    // keeps the live terminal session rather than navigating away from
    // it mid-transfer.
    var link = document.createElement("a");
    link.href = target;
    if (filename) link.download = filename;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  function showTransferNotice(title, message, retry) {
    var existing = document.getElementById("transfer-panel");
    if (existing) existing.remove();

    var panel = document.createElement("div");
    panel.id = "transfer-panel";
    panel.className = "transfer-panel";
    var card = document.createElement("div");
    card.className = "transfer-card";
    var heading = document.createElement("h2");
    heading.textContent = title;
    var body = document.createElement("p");
    body.className = "transfer-status";
    // textContent, never innerHTML: the reason is the server's text.
    body.textContent = message;
    card.appendChild(heading);
    card.appendChild(body);
    var buttons = document.createElement("p");
    function close() {
      panel.remove();
      term.focus();
    }
    if (retry) {
      var again = document.createElement("button");
      again.type = "button";
      again.textContent = "Try again";
      again.addEventListener("click", function () { close(); retry(); });
      buttons.appendChild(again);
    }
    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.textContent = "Close";
    dismiss.addEventListener("click", close);
    buttons.appendChild(dismiss);
    card.appendChild(buttons);
    panel.appendChild(card);
    document.body.appendChild(panel);
    dismiss.focus();
  }

  function openUploadPanel(url) {
    url = transferUrl(url);
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
          if (!response.ok) {
            return response.text().then(function (text) {
              var rejection = new Error(text || ("HTTP " + response.status));
              // The server has already spent the single-use token by the
              // time it rejects, so a retry here can only ever 404
              // (issue #475 review). Marked so the catch below does not
              // invite one.
              rejection.spent = true;
              throw rejection;
            });
          }
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
          inFlight = null;
          var detail = error && error.message ? error.message : error;
          if (error && error.spent) {
            // Nothing to retry with: tell them how to get another link.
            status.textContent = detail + " Ask the BBS for a new link.";
            input.disabled = true;
            return;
          }
          // A rejected fetch does not prove the request never arrived
          // (issue #475 review, unaddressed at merge): the connection can
          // drop after the POST redeemed its token, or after the file was
          // stored but before the response came back. Offering the same
          // link again would 404 at best, and at worst would hide an
          // upload that actually succeeded -- so point them at the
          // listing and at a fresh link instead of claiming this one
          // still works.
          status.textContent =
            "Upload failed: " + detail +
            " It may still have arrived \u2014 check the file listing, and ask the BBS" +
            " for a new link if it did not.";
          // Repaint too (Codex review of #508). This branch sends the
          // caller to the listing, and the listing behind the panel is
          // the page queried *before* the upload -- so without this the
          // advice points at stale evidence, the file that did arrive is
          // missing from it, and the obvious conclusion is to upload it
          // again. The success path already does this; the ambiguous
          // case needs it more, not less.
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "key", data: "\f" }));
          }
          input.disabled = true;
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
