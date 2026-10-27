# figma-bridge

Let an AI agent (Claude Code or any MCP client) build and edit designs in the **Figma desktop app**.
A small Figma development plugin runs in the open file and executes Plugin API JavaScript sent by a local
MCP server. Everything stays on your machine: no Figma REST API, no cloud MCP, **no rate limits**, and the
plugin can use the fonts installed on your computer.

```
 agent ──stdio (MCP)──► figma-bridge mcp ──hosts──► relay (FastAPI, localhost:3789)
                                                      ▲            │
                                    POST /result      │            │ GET /next (long poll)
                                                      │            ▼
                                        Figma desktop ─ "Figma Bridge" plugin (ui.html ⇄ code.js ⇄ Plugin API)
```

- **`plugin/`**: the Figma development plugin. `ui.html` long-polls the relay; `code.js` runs each job with
  the Plugin API and sends the answer back. It can only reach `http://localhost:3789`.
- **`figma_bridge/relay.py`**: FastAPI app. It queues jobs for the plugin and accepts only signed requests (see Security).
- **`figma_bridge/mcp_server.py`**: the MCP server (stdio, official `mcp` Python SDK v2). It hosts the relay
  in-process. If another session already hosts it, it uses that relay, and takes over if that session exits.
- **`figma_bridge/cli.py`**: the `figma-bridge` command: `serve`, `mcp`, `status`, `run`, `screenshot`, `image`, `token`.

## Setup

1. **Environment** (conda):

   ```bash
   conda env create -f environment.yml
   conda activate figma-bridge
   ```

   This installs Python 3.12, FastAPI, uvicorn, httpx, mcp 2.x and pytest, plus this package in editable mode.

2. **Token** (shared secret between the plugin and the relay; stored in `~/.figma-bridge/token`):

   ```bash
   figma-bridge token --show
   ```

3. **Plugin**: in the Figma **desktop** app (development plugins do not run in the browser):
   - open any design file;
   - choose *Plugins → Development → Import plugin from manifest…* and pick `plugin/manifest.json`;
   - run *Plugins → Development → Figma Bridge*, paste the token and press **Connect**. The token is remembered;
     later runs connect on their own.

   Keep the plugin window open while the agent works. It shows the connection state and the last jobs.
   **Pause** stops it taking jobs; closing it disconnects the agent.

   **One file at a time.** If you run the plugin in a second file, that file takes over the bridge, and the first
   one says so. Click **Connect** in a plugin window to take the bridge back.

4. **Register the MCP server** with your agent. For Claude Code:

   ```bash
   claude mcp add figma-bridge --scope user -- "<conda env path>/python.exe" -m figma_bridge mcp
   ```

   Find the env path with `conda env list` (on Windows, typically `C:\Users\<you>\.conda\envs\figma-bridge\python.exe`).
   Start a new agent session afterwards so the tools load.

## Tools (MCP)

| Tool | What it does |
|---|---|
| `figma_status` | Whether the plugin is connected; the open file, page and selection. Call it first. |
| `figma_run` | Runs JavaScript with the full [Figma Plugin API](https://www.figma.com/plugin-docs/) in the open file. Top-level `await`; `return` sends JSON back (nodes as `{id, type, name}`, bytes as `{$bytes: base64}`); `input` passes data in; `console.log` output is returned. |
| `figma_screenshot` | Renders a node (default: the selection; not a whole page) to PNG/JPG and returns the image. It can also save it to a `.png`/`.jpg` file; existing files are kept unless `overwrite` is true. |
| `figma_add_image` | Adds a local PNG/JPG/GIF up to 4096 px per side (as an image fill) or SVG (as vectors) and returns the `imageHash` so `figma_run` code can reuse it. Figma cannot import WebP. |

Code passed to `figma_run` follows the usual plugin rules:
- **Async page APIs.** The plugin uses `documentAccess: dynamic-page`, so use `getNodeByIdAsync`,
  `setCurrentPageAsync` and `loadAsync`.
- **Fonts.** Load each font before editing text with `figma.loadFontAsync`.
- **Colours** are 0–1 floats.
- **Fills and strokes** are read-only arrays: build a new array and assign it.
- **Time limit.** Code that does not finish within its timeout (default 60 s) is abandoned, and the bridge moves
  on. The abandoned code may keep running inside Figma.

## CLI

```bash
figma-bridge status                               # connection + open file
figma-bridge run script.js --input @data.json     # run Plugin API code (or `-` for UTF-8 stdin)
figma-bridge screenshot 12:34 --scale 2 --out frame.png
figma-bridge image logo.png --node 12:40 --mode FIT
figma-bridge serve                                # run the relay alone (e.g. to use only the CLI)
```

When no MCP server is running, CLI commands host the relay themselves and wait up to 15 s for the plugin to reconnect.
In Windows PowerShell, pass JSON with `--input @file.json`: PowerShell 5.1 mangles quotes in native arguments.
Prefer a file path over stdin there too.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FIGMA_BRIDGE_PORT` | `3789` | Relay port. **If you change it, also change `devAllowedDomains` in `plugin/manifest.json` and `RELAY` in `plugin/ui.html`** (Figma requires a host name there, not an IP). |
| `FIGMA_BRIDGE_TOKEN` | *(file)* | Overrides the token file (at least 32 characters). |
| `FIGMA_BRIDGE_HOME` | `~/.figma-bridge` | Where the token file lives. |

## Security

- **Localhost only.** The relay listens on `127.0.0.1` and `::1` only, with an exclusive bind on Windows.
  If another program already holds `[::1]:3789`, the relay refuses to start: the plugin connects to `localhost`,
  which tries `::1` first, so its traffic would go to that program.
- **The token never crosses the wire.** Every request carries a timestamp, a single-use nonce and an HMAC-SHA256
  signature made with the token. The signature covers the method, path, plugin session and body. The relay rejects
  unsigned, wrong-token, replayed, stale or altered requests, so other programs and web pages cannot send code to
  Figma or forge results.
- **Responses are signed too.** The plugin and the CLI/MCP clients check every response. A program that grabs
  the port while no relay is running can neither feed the plugin code nor learn the token.
- **Plugin network access.** The plugin may only contact `http://localhost:3789`.
- **Code runs with your access.** Anything the agent sends runs inside Figma with your permissions on the open file,
  exactly like a plugin you wrote. Only connect agents you trust.
- **Rotating the token.** Run `figma-bridge token --reset`. Running relays pick the new token up at once, and the
  old one stops working. Paste the new one into the plugin and click Connect.
- **Local files only.** `save_path` and image paths must be drive-letter or relative paths. UNC, device and
  NT-namespace paths are refused before anything touches the filesystem, so no network share is ever contacted.
  `save_path` also needs an extension matching the image format, and never overwrites a file unless asked.

## Tests

```bash
conda activate figma-bridge
pytest
```

The tests cover:
- **The relay:** job round-trip, hosting hand-over between processes, signatures and replay protection, CORS and the
  private-network preflight, 413 with CORS, timeouts, cancelled jobs being withdrawn, two Figma files taking over
  from each other, a port squatter being refused, and weak tokens.
- **The MCP server:** end to end over stdio, against a fake plugin.
- **`plugin/code.js`:** run under Node in a sandbox where, as in Figma, the `Function`/`AsyncFunction` constructors
  are blocked.
- **`plugin/ui.html`:** its real script, run under Node against the real relay. This covers the HMAC
  implementation against Python's, connecting, running jobs, hand-over between files, a wrong token, and refusing a squatter.

- **Hardening:** `[::1]` port squatting, re-connecting after a relay hand-over, repairing broken UTF-16 text,
  refusing NaN payloads, signatures bound to the body and session, strict auth headers, network-path refusal,
  JPEG size parsing, and UTF-16 script files.

The Node-based tests are skipped when Node.js is not installed.

## Limits

- The Figma desktop app must be running, with the plugin open in the file you want to edit.
- One job runs at a time, in order.
- Images: PNG/JPG/GIF up to 4096 px per side (a Figma limit) and 40 MB. Results: up to about 60 MB per job.
