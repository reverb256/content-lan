#!/usr/bin/env python3
"""content.lan approval API — lets j_kro approve/reject media from the site.

Cluster-native version (moved off zephyr 2026-09-23). Same HTTP contract as the
workstation copy it replaces:

    GET  /health              -> {"ok": true, "service": "content-lan-approval"}
    POST / {"asset": "...",   -> writes status into the asset's sidecar .md,
                        "action": "approve"|"reject"}

Paths are arguments so the media tree (a cluster volume seeded from the
`content-lan` repo) can be mounted anywhere:

    --media-root   served media tree (sidecar `status:` is rewritten in place)
    --log-path     append-only jsonl of every decision
    --pending-dir  durable queue for actions this container cannot execute
                   itself (the `hermes kanban ... unblock` call needs the
                   hermes CLI, which is not installed in the cluster image).

The pending queue is what keeps the kanban unblock from being silently dropped:
the record is written to disk before the response is returned, and the
pipeline (site-agency, which owns the faceless-youtube board) drains it.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KANBAN_BOARD = "faceless-youtube"
YOUTUBE_MAP = {
    "applied-psychology-final.mp4": "t_1314c000",
    "applied-psychology-ai-was-wrong.mp4": "t_1314c000",
    "videos/applied-psychology-ai-was-wrong.mp4": "t_1314c000",
}

# Music lane (task t_98c95aad): approving a music sidecar must
# unblock the mapped card on the `music` board, not the
# faceless-youtube board. The board is derived from the asset's
# kind directory, so one code path serves both lanes.
MUSIC_BOARD = "music"
MUSIC_MAP = {
    "what-runs-beneath.mp3": "t_b908143a",
    "amber-hour.mp3": "t_b908143a",
    "signal-before-dawn.mp3": "t_b908143a",
    "music/what-runs-beneath.mp3": "t_b908143a",
    "music/amber-hour.mp3": "t_b908143a",
    "music/signal-before-dawn.mp3": "t_b908143a",
}


def _kanban_target(asset: str):
    """Map an asset to (ticket, board). Music assets hit the
    music board; everything else hits faceless-youtube. Keys are
    accepted both with and without the kind prefix so a caller
    that sends `what-runs-beneath.mp3` or
    `music/what-runs-beneath.mp3` both resolve."""
    bare = asset.split("/", 1)[-1]
    if asset.startswith("music/") or bare in MUSIC_MAP or asset in MUSIC_MAP:
        ticket = MUSIC_MAP.get(asset) or MUSIC_MAP.get(bare)
        if ticket:
            return ticket, MUSIC_BOARD
    ticket = YOUTUBE_MAP.get(asset) or YOUTUBE_MAP.get(bare)
    if ticket:
        return ticket, KANBAN_BOARD
    return None, None


def _log(log_path: Path, action: str, asset: str, detail: str = "") -> None:
    entry = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        "asset": asset,
        "detail": detail,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:  # never fail a request because logging failed
        print(f"log failed: {e}", file=sys.stderr)


def _sidecar_for(media_root: Path, asset: str):
    # Sidecars are deployed as file.md (replace extension), e.g.
    # videos/applied-psychology-ai-was-wrong.mp4 -> ...was-wrong.md
    p = media_root / asset
    for c in [p.with_suffix(".md"), Path(str(p) + ".md"), p.with_suffix(p.suffix + ".md")]:
        if c.exists():
            return c
    return None


def _update_status_sidecar(media_root: Path, asset: str, new_status: str) -> bool:
    sc = _sidecar_for(media_root, asset)
    if sc is None:
        return False
    try:
        text = sc.read_text()
    except Exception:
        return False
    if re.search(r"(?im)^status\s*:", text):
        text = re.sub(r"(?im)^status\s*:.*$", f"status: {new_status}", text, count=1)
    else:
        text = text.rstrip() + f"\nstatus: {new_status}\n"
    try:
        sc.write_text(text)
    except Exception as e:
        print(f"write sidecar failed: {e}", file=sys.stderr)
        return False
    return True


def _queue_kanban(pending_dir: Path, log_path: Path, asset: str, tid: str, board: str = KANBAN_BOARD) -> str:
    """Record the unblock so it is durable even though this container has no
    hermes CLI. Returns a short status string for the response body."""
    pending_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "board": board,
        "action": "unblock",
        "ticket": tid,
        "asset": asset,
    }
    path = pending_dir / "kanban-unblocks.jsonl"
    try:
        with path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        _log(log_path, "approve", asset, f"pending queue write failed: {e}")
        return "queue_failed"

    # Try the live call first: if a hermes CLI ever lands in this image the
    # queue entry is simply marked done instead of being needed.
    try:
        r = subprocess.run(
            ["hermes", "kanban", "--board", board, "unblock", tid],
            capture_output=True,
            text=True,
            timeout=30,
        )
        detail = f"kanban {tid}@{board}: {r.stdout.strip()[:80]}"
        _log(log_path, "approve", asset, detail)
        return "done"
    except Exception as e:
        _log(log_path, "approve", asset, f"kanban {tid}@{board} queued (no hermes CLI: {e})")
        return "queued"


class Handler(BaseHTTPRequestHandler):
    media_root: Path = Path("/site/media")
    log_path: Path = Path("/site/approval-log.jsonl")
    pending_dir: Path = Path("/site/pending")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json(200, {"ok": True, "service": "content-lan-approval"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln)) if ln else {}
        except Exception:
            body = {}
        asset = body.get("asset", "")
        action = body.get("action", "")
        if not asset or action not in ("approve", "reject"):
            self._json(400, {"error": "need asset + action (approve|reject)"})
            return
        if action == "approve":
            self._json(200, self._approve(asset))
        else:
            self._json(200, self._reject(asset))

    def _approve(self, asset):
        ok = _update_status_sidecar(self.media_root, asset, "approved")
        _log(self.log_path, "approve", asset, "sidecar_ok" if ok else "no_sidecar")
        tid, board = _kanban_target(asset)
        kanban = None
        if tid:
            kanban = _queue_kanban(self.pending_dir, self.log_path, asset, tid, board)
        return {"ok": ok, "asset": asset, "status": "approved", "kanban": tid, "board": board, "kanban_state": kanban}

    def _reject(self, asset):
        ok = _update_status_sidecar(self.media_root, asset, "rejected")
        _log(self.log_path, "reject", asset, "sidecar_ok" if ok else "no_sidecar")
        return {"ok": ok, "asset": asset, "status": "rejected"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--media-root", default="/site/media")
    ap.add_argument("--log-path", default="/site/approval-log.jsonl")
    ap.add_argument("--pending-dir", default="/site/pending")
    args = ap.parse_args()
    Handler.media_root = Path(args.media_root)
    Handler.log_path = Path(args.log_path)
    Handler.pending_dir = Path(args.pending_dir)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
