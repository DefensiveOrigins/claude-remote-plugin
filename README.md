# claude-remote-plugin

# remote-prompt

This project sends prompts between Claude Code and a remote host over plain TCP. The default port is 81.

```
Claude Code ──(plugin: MCP stdio)── rp_client.ps1 ──TCP:81 / JSON lines──▶ rp_server.py (remote host)
  (Windows, PowerShell 5.1)                                              ├─ console: operator types replies
                                                                         ├─ claude:  piped to `claude -p`
                                                                         └─ echo:    connectivity test
```

- **Plugin side:** the Windows machine running Claude Code needs only built-in Windows PowerShell 5.1. Python isn't required.
- **Server side:** needs Python 3.8 or later. It uses only the standard library, so you don't need to `pip install` anything.

```
remote-prompt/
├── .claude-plugin/marketplace.json   local marketplace, so you can install the plugin
├── plugin/                           the Claude Code plugin
│   ├── .claude-plugin/plugin.json
│   ├── .mcp.json                     launches powershell.exe -File rp_client.ps1 -Mcp
│   ├── scripts/rp_client.ps1         MCP server + CLI test client
│   └── commands/                     /rp-send /rp-post /rp-inbox /rp-ping
└── server/rp_server.py               the recipient server
```

## Recipient server

```bash
python3 rp_server.py --bind 0.0.0.0 --port 81 --token 'change-me' --allow 10.0.0.0/24 --mode console
```

| Flag | Env | Default | Purpose |
|---|---|---|---|
| `--bind` | `RP_BIND` | `0.0.0.0` | Local listen address. Use `::` for IPv6. |
| `--port` | `RP_PORT` | `81` | TCP listen port. |
| `--allow` | `RP_ALLOW` | any | Remote client IPs/CIDRs allowed to connect. Repeat the flag or separate entries with commas. |
| `--token` | `RP_TOKEN` | none | Shared secret that every client must send. |
| `--mode` | `RP_MODE` | `console` | `console`, `claude` or `echo` |
| `--claude-cmd` / `--claude-args` | | `claude` / "" | Used only by `--mode claude`, e.g. `--claude-args "--model sonnet"` |
| `--max-concurrent` | | 2 | Maximum number of `claude` runs at once |
| `--prompt-timeout` | | 600 | Maximum time in seconds for one prompt |
| `--no-console` | | | Run without stdin, e.g. as a service. Can't be used with `console` mode. |

Operator console commands (put `\n` in text for newlines):
`/reply <id> <text>`, `/r <text>` (replies to the oldest pending prompt), `/send <client> <text>` (queues a message for a client), `/pending`, `/inbox`, `/clients`.

On Linux, port 81 is privileged. Either run the server as root, or grant the capability:
`sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python3))`
On Windows, allow inbound TCP 81 through the firewall.

## Plugin (client side)

Install it in Claude Code:

```
/plugin marketplace add C:\path\to\remote-prompt
/plugin install remote-prompt@remote-prompt-local
```

Set the target before you launch Claude Code. Env vars are expanded in `.mcp.json`:

| Env | Default | |
|---|---|---|
| `RP_HOST` | `127.0.0.1` | Remote server host/IP |
| `RP_PORT` | `81` | Remote server port |
| `RP_TOKEN` | "" | Must match the server's `--token` |
| `RP_CLIENT_NAME` | computer name | This client's mailbox name |
| `RP_TIMEOUT` | `600` | Seconds to wait for a prompt reply |

Each tool call and slash command can also override the host and port:

- `/rp-send @10.0.0.5:81 summarize the last deploy log`: sends a prompt and waits for the response
- `/rp-post to:bob please review PR 12`: queues a message for another client, `bob`. Without `to:`, the message goes to the server operator.
- `/rp-inbox @10.0.0.5`: receives the prompts and messages queued for this client
- `/rp-ping @10.0.0.5:8181`

The MCP tools are `remote_ping`, `remote_prompt`, `remote_post` and `remote_fetch`.

`.mcp.json` runs the client with `-ExecutionPolicy Bypass`, so it works even when the script is unsigned or was downloaded. If Group Policy enforces an execution policy, that policy overrides `Bypass`. In that case, sign `rp_client.ps1` or get it allowed.

## Testing without Claude Code

On the server host:

```bash
python3 server/rp_server.py --bind 0.0.0.0 --port 8181 --mode echo --token t
```

On the Windows Claude Code host, in PowerShell:

```powershell
$c = ".\plugin\scripts\rp_client.ps1"; $env:RP_TOKEN = "t"
powershell -NoProfile -ExecutionPolicy Bypass -File $c -RemoteHost 10.0.0.5 -Port 8181 ping
powershell -NoProfile -ExecutionPolicy Bypass -File $c -RemoteHost 10.0.0.5 -Port 8181 prompt hello there
$env:RP_CLIENT_NAME = "bob";   powershell -NoProfile -ExecutionPolicy Bypass -File $c -RemoteHost 10.0.0.5 -Port 8181 post -To alice "hi alice"
$env:RP_CLIENT_NAME = "alice"; powershell -NoProfile -ExecutionPolicy Bypass -File $c -RemoteHost 10.0.0.5 -Port 8181 fetch
```

## Protocol

The client sends one UTF-8 JSON object per line, and the server answers each request with one JSON line. Each message is limited to 1 MiB.

```json
{"op":"ping"}
{"op":"prompt","text":"...","timeout":600}
{"op":"post","text":"...","to":"server|<client>"}
{"op":"fetch","limit":50}
```

Every request carries `client` and, if a token is set, `token`.

## Security notes

- **Traffic is cleartext.** The token and prompts can be read by anyone on the network path. Use the tool on trusted segments, or tunnel it, e.g. `ssh -L 81:127.0.0.1:81` or stunnel/WireGuard.
- **Always set `--token`, and set `--allow` where you can.** The server warns at startup if it's exposed without a token.
- **`--mode claude` lets remote clients drive Claude on the server host.** Restrict its tools through `--claude-args`, e.g. `--allowedTools ""` or a permission mode, and don't use `--dangerously-skip-permissions` there.
- **Received text is untrusted.** The slash commands tell Claude to show responses and inbox messages without acting on them. Keep that behavior if you add automation on top.
- Server state (mailboxes, inbox) is held in memory only and is lost on restart.
