#!/usr/bin/env python3
"""
rp_server.py - Remote Prompt recipient server.

Listens on TCP (default 0.0.0.0:81) for newline-delimited JSON requests from the
remote-prompt Claude Code plugin (or any client speaking the same protocol).
Standard library only; Python 3.8+.

Protocol: one JSON object per line, UTF-8, one JSON reply line per request.
  {"op": "ping"}
  {"op": "prompt", "text": "...", "timeout": 600}   -> {"ok", "id", "response"}
  {"op": "post",   "text": "...", "to": "server"}   -> {"ok", "id", "to"}
  {"op": "fetch",  "limit": 50}                     -> {"ok", "messages", "remaining"}
Every request may carry "token" (shared secret) and "client" (sender name).

Prompt handling modes (--mode):
  console  operator answers each prompt at this server's console (default)
  claude   prompt is piped to `claude -p` on this host and its output returned
  echo     prompt is echoed back (connectivity testing)
"""
import argparse
import hmac
import ipaddress
import json
import logging
import os
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict

MAX_LINE = 1 << 20          # 1 MiB per message
MAX_MAILBOX = 1000          # queued messages kept per recipient
log = logging.getLogger("rp_server")
_print_lock = threading.Lock()


def console_print(text):
    with _print_lock:
        print(text, flush=True)


def new_id():
    return uuid.uuid4().hex[:8]


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.mailboxes = defaultdict(list)   # client name -> messages queued for it
        self.inbox = []                      # messages posted to the server operator
        self.pending = {}                    # prompt id -> console prompt awaiting reply
        self.clients = {}                    # client name -> (ip, last seen)


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State()
        self.allow = [ipaddress.ip_network(a, strict=False) for a in cfg.allow]
        self.claude_slots = threading.Semaphore(cfg.max_concurrent)
        self.address_family = socket.AF_INET6 if ":" in cfg.bind else socket.AF_INET
        super().__init__((cfg.bind, cfg.port), Handler)

    def ip_allowed(self, ip):
        if not self.allow:
            return True
        addr = ipaddress.ip_address(ip.split("%")[0])
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in net for net in self.allow)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        ip = self.client_address[0]
        if not srv.ip_allowed(ip):
            log.warning("rejected connection from %s (not in --allow)", ip)
            return
        self.connection.settimeout(srv.cfg.idle_timeout)
        while True:
            try:
                line = self.rfile.readline(MAX_LINE + 1)
            except OSError:
                break
            if not line:
                break
            if len(line) > MAX_LINE:
                self.reply({"ok": False, "error": "message too large"})
                break
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                self.reply({"ok": False, "error": "invalid JSON"})
                continue
            try:
                resp = dispatch(srv, ip, req)
            except Exception as e:  # never kill the connection thread silently
                log.exception("error handling request from %s", ip)
                resp = {"ok": False, "error": "server error: %s" % e}
            try:
                self.reply(resp)
            except OSError:
                break

    def reply(self, obj):
        self.wfile.write(json.dumps(obj).encode("utf-8") + b"\n")
        self.wfile.flush()


def dispatch(srv, ip, req):
    cfg, st = srv.cfg, srv.state
    if cfg.token and not hmac.compare_digest(str(req.get("token") or "").encode(), cfg.token.encode()):
        log.warning("bad or missing token from %s", ip)
        return {"ok": False, "error": "unauthorized"}

    op = req.get("op")
    client = str(req.get("client") or ip)[:64]
    with st.lock:
        st.clients[client] = (ip, time.time())

    if op == "ping":
        return {"ok": True, "op": "pong", "server": cfg.name, "mode": cfg.mode}

    if op == "prompt":
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "missing 'text'"}
        pid = new_id()
        log.info("prompt %s from %s@%s (%d chars)", pid, client, ip, len(text))
        timeout = min(float(req.get("timeout") or cfg.prompt_timeout), cfg.prompt_timeout)
        return PROMPT_HANDLERS[cfg.mode](srv, pid, client, text, timeout)

    if op == "post":
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "missing 'text'"}
        to = str(req.get("to") or "server")[:64]
        msg = {"id": new_id(), "from": client, "to": to, "text": text, "ts": time.time()}
        with st.lock:
            box = st.inbox if to == "server" else st.mailboxes[to]
            box.append(msg)
            del box[:-MAX_MAILBOX]
        if to == "server":
            console_print("\n[message %s] from %s@%s:\n%s" % (msg["id"], client, ip, text))
        log.info("post %s from %s to %s", msg["id"], client, to)
        return {"ok": True, "id": msg["id"], "to": to}

    if op == "fetch":
        limit = max(1, min(int(req.get("limit") or 50), MAX_MAILBOX))
        with st.lock:
            box = st.mailboxes.get(client, [])
            msgs, rest = box[:limit], box[limit:]
            st.mailboxes[client] = rest
        return {"ok": True, "messages": msgs, "remaining": len(rest)}

    return {"ok": False, "error": "unknown op %r" % (op,)}


# --- prompt handlers --------------------------------------------------------

def handle_echo(srv, pid, client, text, timeout):
    return {"ok": True, "id": pid, "response": "echo from %s: %s" % (srv.cfg.name, text)}


def handle_claude(srv, pid, client, text, timeout):
    cfg = srv.cfg
    cmd = [cfg.claude_cmd, "-p"] + shlex.split(cfg.claude_args)
    with srv.claude_slots:
        try:
            # Prompt goes over stdin so it never touches the command line.
            proc = subprocess.run(cmd, input=text, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "id": pid, "error": "claude timed out after %ss" % timeout}
    if proc.returncode != 0:
        return {"ok": False, "id": pid,
                "error": proc.stderr.strip() or "claude exited with %d" % proc.returncode}
    return {"ok": True, "id": pid, "response": proc.stdout.strip()}


def handle_console(srv, pid, client, text, timeout):
    st = srv.state
    entry = {"event": threading.Event(), "response": None, "client": client, "text": text}
    with st.lock:
        st.pending[pid] = entry
    console_print("\n[prompt %s] from %s:\n%s\n  -> /reply %s <text>   (or /r <text> for the oldest)"
                  % (pid, client, text, pid))
    answered = entry["event"].wait(timeout)
    with st.lock:
        st.pending.pop(pid, None)
    if not answered:
        return {"ok": False, "id": pid, "error": "no operator reply within %ss" % timeout}
    return {"ok": True, "id": pid, "response": entry["response"]}


PROMPT_HANDLERS = {"echo": handle_echo, "claude": handle_claude, "console": handle_console}


# --- operator console -------------------------------------------------------

CONSOLE_HELP = """Operator commands (use \\n in text for newlines):
  /reply <id> <text>     answer a pending prompt
  /r <text>              answer the oldest pending prompt
  /send <client> <text>  queue a message for a client (it receives it via fetch)
  /pending               list prompts awaiting a reply
  /inbox                 show and clear messages posted to the server
  /clients               list clients seen
  /help"""


def console_loop(srv):
    st = srv.state
    console_print(CONSOLE_HELP)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()
        if cmd in ("/reply", "/r"):
            with st.lock:
                if cmd == "/r":
                    pid = next(iter(st.pending), None)
                    text = rest
                else:
                    pid, _, text = rest.partition(" ")
                entry = st.pending.get(pid)
            if not entry:
                console_print("no pending prompt%s" % (" " + pid if pid else ""))
                continue
            entry["response"] = text.replace("\\n", "\n")
            entry["event"].set()
            console_print("replied to %s" % pid)
        elif cmd == "/send":
            client, _, text = rest.partition(" ")
            if not client or not text:
                console_print("usage: /send <client> <text>")
                continue
            msg = {"id": new_id(), "from": "server:" + srv.cfg.name, "to": client,
                   "text": text.replace("\\n", "\n"), "ts": time.time()}
            with st.lock:
                st.mailboxes[client].append(msg)
                del st.mailboxes[client][:-MAX_MAILBOX]
            console_print("queued %s for %s" % (msg["id"], client))
        elif cmd == "/pending":
            with st.lock:
                items = [(k, v["client"], v["text"]) for k, v in st.pending.items()]
            for pid, client, text in items:
                console_print("  %s  %s: %s" % (pid, client, text[:100]))
            if not items:
                console_print("  (none)")
        elif cmd == "/inbox":
            with st.lock:
                msgs, st.inbox = st.inbox, []
            for m in msgs:
                console_print("  [%s] %s: %s" % (m["id"], m["from"], m["text"]))
            if not msgs:
                console_print("  (empty)")
        elif cmd == "/clients":
            with st.lock:
                items = sorted(st.clients.items())
            for name, (ip, seen) in items:
                console_print("  %s  %s  last seen %s" % (name, ip, time.strftime("%H:%M:%S", time.localtime(seen))))
            if not items:
                console_print("  (none)")
        else:
            console_print(CONSOLE_HELP)


# --- entry point ------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Remote Prompt recipient server")
    p.add_argument("--bind", default=os.environ.get("RP_BIND", "0.0.0.0"),
                   help="local address to listen on (default 0.0.0.0; use :: for IPv6) [env RP_BIND]")
    p.add_argument("--port", type=int, default=int(os.environ.get("RP_PORT", 81)),
                   help="TCP port to listen on (default 81) [env RP_PORT]")
    p.add_argument("--allow", action="append", default=[],
                   help="allowed client IP or CIDR; repeat or comma-separate. Default: any [env RP_ALLOW]")
    p.add_argument("--token", default=os.environ.get("RP_TOKEN", ""),
                   help="shared secret clients must send [env RP_TOKEN]")
    p.add_argument("--mode", choices=sorted(PROMPT_HANDLERS), default=os.environ.get("RP_MODE", "console"),
                   help="how prompts are answered (default console) [env RP_MODE]")
    p.add_argument("--claude-cmd", default="claude", help="claude executable for --mode claude")
    p.add_argument("--claude-args", default="",
                   help='extra args for claude -p, e.g. "--model sonnet --allowedTools Read"')
    p.add_argument("--max-concurrent", type=int, default=2, help="parallel claude runs (default 2)")
    p.add_argument("--prompt-timeout", type=float, default=600, help="max seconds per prompt (default 600)")
    p.add_argument("--idle-timeout", type=float, default=300, help="drop idle connections after N seconds")
    p.add_argument("--name", default=socket.gethostname(), help="server name reported to clients")
    p.add_argument("--no-console", action="store_true", help="don't read operator commands from stdin")
    p.add_argument("-v", "--verbose", action="store_true")
    cfg = p.parse_args()

    allow = cfg.allow or ([os.environ["RP_ALLOW"]] if os.environ.get("RP_ALLOW") else [])
    cfg.allow = [a.strip() for item in allow for a in item.split(",") if a.strip()]
    for a in cfg.allow:
        try:
            ipaddress.ip_network(a, strict=False)
        except ValueError:
            p.error("invalid --allow entry: %s" % a)
    if cfg.mode == "console" and cfg.no_console:
        p.error("--mode console needs the console; drop --no-console or pick another mode")
    if cfg.mode == "claude":
        resolved = shutil.which(cfg.claude_cmd)
        if not resolved:
            p.error("claude executable not found: %s (set --claude-cmd)" % cfg.claude_cmd)
        cfg.claude_cmd = resolved
    return cfg


def main():
    cfg = parse_args()
    logging.basicConfig(level=logging.DEBUG if cfg.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if cfg.bind not in ("127.0.0.1", "::1", "localhost") and not cfg.token:
        log.warning("listening on %s without --token: anyone who can reach this port can send prompts", cfg.bind)
    if cfg.mode == "claude":
        log.warning("--mode claude runs remote prompts through Claude on this host; restrict with --token/--allow")

    try:
        srv = Server(cfg)
    except OSError as e:
        sys.exit("cannot listen on %s:%d: %s" % (cfg.bind, cfg.port, e))
    log.info("listening on %s:%d mode=%s allow=%s auth=%s", cfg.bind, cfg.port, cfg.mode,
             ",".join(cfg.allow) or "any", "token" if cfg.token else "none")

    if not cfg.no_console:
        threading.Thread(target=console_loop, args=(srv,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
