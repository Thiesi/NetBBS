"""
QWKnet (issues #1481 and #1482, design doc §6.11): this node on a QWK
network, as a node of a hub such as Synchronet's Vertrauen (DOVE-Net), and
as a hub other systems call.

- `networks`: the network records, the conference <-> board mapping and the
  node accounts a hub serves.
- `exchange`: what moves -- a hub's packet tossed onto boards, a node's reply
  packet tossed and passed on, and the packets and reply packets built from
  what waits -- with the duplicate, loop and held-message rules.
- `transport`: the calls to a hub, over FTP/FTPS (passive only) or over
  HTTP/HTTPS the way Synchronet's `qnet-http` makes them.
- `mailer`: the background task that calls each network's hub.
- `hub`: the `/qwk.ssjs` endpoint on the web listener that nodes call.

The packet and reply-packet formats are `netbbs.qwk`, shared with QWK
offline mail for callers (§6.10).
"""
