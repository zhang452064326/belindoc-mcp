# Belindoc MCP Server

<!-- mcp-name: com.belindoc/belindoc-mcp -->

[English](README.md) | [简体中文](README.zh-CN.md)

An MCP server for the [Belindoc](https://belindoc.com/mcp) translation API. Translate PDF,
Word, Excel, Markdown and image files, translate videos, and rewrite video subtitles — from
Claude, Cursor, Codex or any other MCP client.

- **Website:** https://belindoc.com/mcp
- **PyPI:** [`belindoc-mcp`](https://pypi.org/project/belindoc-mcp/)
- **Remote endpoint:** `https://mcp.belindoc.com/api/mcp` (Streamable HTTP)

## Two ways to run it

| | stdio | Remote HTTP |
|---|---|---|
| Entry point | `belindoc-mcp` | `trans-mcp-http` |
| Runs on | the user's own machine | a server shared by many users |
| API key | read from `BELINDOC_API_KEY` | each client sends `Authorization: Bearer <key>`; the server stores no keys |
| Transport | stdio | Streamable HTTP (SSE + `Mcp-Session-Id`) |

Both modes expose the same tools and behave the same way, including asking the user for
confirmation directly (elicitation) and sending progress notifications while waiting.
Self-hosting the HTTP mode is covered in [DEPLOY.md](DEPLOY.md).

## Installation

Install from PyPI — no need to clone the source:

```bash
uvx belindoc-mcp       # quick test run; client configs use this same command, no pre-install needed
# or
pipx install belindoc-mcp
```

`uvx` comes with uv. If you don't have uv yet:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`brew install uv` or `pip install uv` work too.

**Open a new terminal after installing uv** — `uv` only lands on `PATH` in a new shell.
Skipping this is by far the most common reason clients later fail with "uvx not found".

The `pipx` route does not need uv.

To install from source (for development), see [Development](#development).

## Environment variables

| Variable | Mode | Description |
|------|--------|------|
| `BELINDOC_API_KEY` | stdio | Required. Format: `ft_` + 40 random characters (43 in total) |
| `BELINDOC_API_BASE_URL` | both | Upstream API. Defaults to production `https://belindoc.com/api`; only set it to target another environment |
| `MCP_HOST` / `MCP_PORT` | HTTP | Listen address and port, default `0.0.0.0:8080` |
| `MCP_PATH` | HTTP | MCP endpoint path, default `/mcp` |
| `MCP_LOCALE` | both | Language of user-facing messages, default `zh`. See [Output language](#output-language) |

HTTP mode does **not** read `BELINDOC_API_KEY` — don't put a real key in the server's `.env`.
See [.env.example](.env.example) for the annotated list.

### Getting an API key

Sign in at https://belindoc.com → Open Platform → API Keys → Create.

## Client setup

### stdio

```json
{
  "mcpServers": {
    "belindoc-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["belindoc-mcp"],
      "env": {
        "BELINDOC_API_KEY": "ft_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "BELINDOC_API_BASE_URL": "https://belindoc.com/api"
      }
    }
  }
}
```

`BELINDOC_API_BASE_URL` is set to its default here and can be omitted.

Claude Code and Codex can add it from the command line instead:

```bash
# Claude Code
claude mcp add belindoc-mcp \
  -e BELINDOC_API_KEY=ft_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  -- uvx belindoc-mcp

# Codex
codex mcp add belindoc-mcp \
  --env BELINDOC_API_KEY=ft_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  -- uvx belindoc-mcp
```

The only difference is how env vars are passed: `-e` for `claude`, `--env` for `codex`.
Everything after `--` is the command to run — don't drop it.

`uvx` fetches the package and creates an isolated environment on its own, so users don't
need to pre-install this project or deal with paths — they only need uv itself (see
[Installation](#installation)).

**When installed from source**, `command` must be an absolute path: after `pip install -e .`
the venv contains a `belindoc-mcp` executable — use its full path (e.g.
`/path/to/trans-mcp/.venv/bin/belindoc-mcp`). Clients don't start a login shell, so a bare
command name usually won't be found on `PATH`.

### Other clients

The stdio config is the same across clients. Only the config file location and the top-level
key differ:

| Client | Config file | Top-level key |
|--------|----------|--------|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` | `mcpServers` |
| Claude Code | `.mcp.json` in the project root (or `claude mcp add`) | `mcpServers` |
| Codex | `~/.codex/config.toml` (TOML; or `codex mcp add`) | `mcp_servers` |
| Cursor | `.cursor/mcp.json` in the project, or global `~/.cursor/mcp.json` | `mcpServers` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| VS Code | `.vscode/mcp.json` in the project | `servers` |

Clients change these paths from time to time — check your client's current docs.

If it won't start, check two things first: whether `uvx` is on the `PATH` the client sees
(most often: uv was installed but the terminal wasn't reopened), and whether the key is
correct.

### Remote HTTP

```json
{
  "mcpServers": {
    "belindoc-mcp": {
      "type": "http",
      "url": "https://mcp.belindoc.com/api/mcp",
      "headers": {
        "Authorization": "Bearer ft_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

The value of `type` varies by client: Claude Code uses `http`; check other clients' docs.
"Streamable HTTP" is the protocol name, not a config value.

Codex CLI can't send custom headers for HTTP MCP servers, only a bearer token. In
`~/.codex/config.toml`:

```toml
[mcp_servers.belindoc-mcp]
url = "https://mcp.belindoc.com/api/mcp"
bearer_token_env_var = "BELINDOC_API_KEY"
```

### After connecting

Call `get_account_status` once to check the key works and see your balance. To find out
whether your client supports server-side confirmation prompts (which decides whether video
submission takes one call or two), start the server with `MCP_DEBUG_TOOLS=1` and call
`probe_elicitation` — it doesn't translate, submit or charge anything.

## Typical workflows

**Documents:** `upload_document` to get a presigned URL → upload with the returned
`uploadCommand` → (PDF only) `check_pdf_ocr` to detect scanned PDFs → `translate_document`
→ `wait_for_translation` → `get_document_translation_result` for the download link.

**Videos:** `upload_video` → upload → `calculate_video_translation_quota` to estimate cost →
`translate_video` (two-step confirmation, see below) → `wait_for_video_translation`.
To edit subtitles and re-render: `get_video_subtitles` → `calculate_rewrite_quota` →
`rewrite_video_subtitles` → `get_video_rewrite_status`.

The caller runs the returned `uploadCommand` itself; the server never touches files on the
user's machine. Download links are signed URLs — don't change a single character after the
`?`, or you'll get a 403.

## Tools

### Account and metadata
| Tool | Description |
|------|------|
| `get_supported_languages` | Supported languages (79, language code → display name) |
| `get_model_list` | Translation models available to the account |
| `get_account_status` | Remaining quota, membership tier and limits (max video length / concurrency / file size) |

### Document translation
| Tool | Description |
|------|------|
| `upload_document` | Get a presigned upload URL for a document |
| `check_pdf_ocr` | Check whether an uploaded PDF is scanned / dual-layer |
| `translate_document` | Submit a document translation task |
| `wait_for_translation` | Wait for a task, returning whenever progress changes |
| `get_document_translation_status` | Get one task's status |
| `get_document_translation_result` | Get the translated file's download link |
| `list_document_translations` | List tasks (paginated) |
| `get_document_translation_by_batch` | Get tasks by batch number |

### Video translation
| Tool | Description |
|------|------|
| `upload_video` | Get a presigned upload URL for a video |
| `calculate_video_translation_quota` | Estimate the quota cost without charging |
| `translate_video` | Submit a video translation task (charges quota; two-step confirmation) |
| `wait_for_video_translation` | Wait for a task, returning whenever progress changes |
| `get_video_translation_status` | Get one task's status |
| `list_video_translations` | List tasks (paginated; last 15 days only) |
| `cancel_video_translation` | Cancel a task |

### Subtitle rewriting
| Tool | Description |
|------|------|
| `get_video_subtitles` | Get download URLs for source and translated subtitles |
| `calculate_rewrite_quota` | Estimate the rewrite cost without charging |
| `rewrite_video_subtitles` | Re-render the video with edited subtitles (charges quota; two-step confirmation) |
| `get_video_rewrite_status` | Get rewrite progress |

### Diagnostics

Hidden by default; set `MCP_DEBUG_TOOLS=1` to expose it.

| Tool | Description |
|------|------|
| `probe_elicitation` | Self-check: does this client support elicitation? No translation, no submission, no charge |

## Charge confirmation

`translate_video` and `rewrite_video_subtitles` spend quota, so submitting takes **two
steps** — the first call never submits:

- If the client supports **elicitation**, the server asks the user directly and one call is
  enough.
- Otherwise it falls back to a **confirmation token**: the first call returns 409 with a
  message for the user and a menu (combinations of dubbing × subtitles, each with its cost and
  a `confirmToken`). Show the menu to the user as-is, then call again with the `confirmToken`
  of the option they picked — only that call submits.

A plain `user_confirmed=true` flag isn't trusted because the model always fills it in itself;
the server can't verify that a human was actually asked.

## Output language

Text meant to be relayed to the user (task status, output descriptions, progress lines,
failure reasons, download notes) is available in nine languages: `zh` / `zh-Hant` / `en` /
`ja` / `ko` / `de` / `fr` / `ru` / `ar`. Tool descriptions and instructions for the model are
always in Chinese — they're written for the model.

Priority: the tool's `locale` argument > server `MCP_LOCALE` > `zh`.

## Troubleshooting

### Authentication failed (10004)

The API key is wrong, unregistered, or malformed (it must start with `ft_` and be 43
characters). Check `echo $BELINDOC_API_KEY`, then check the key's status on the platform.

### Key errors (30306 / 30307 / 30308 / 30309 / 30312)

The upstream returns these with HTTP 200 and the code in the body. The tools turn them into
an actionable sentence (key not copied fully / disabled and needs re-enabling / expired / IP
not allow-listed / contact support), and state clearly that **retrying, changing arguments
or re-uploading won't help**. Only 30311 should be retried with backoff.

### Endpoint not found (404)

The response says "endpoint X does not exist at the current service URL (Y)". This isn't a
network problem — the feature isn't deployed in that environment, or
`BELINDOC_API_BASE_URL` points to the wrong one. Retrying won't help.

### Connection timeout

Check that the backend is reachable and no firewall is blocking it.

## Development

```bash
cd /path/to/trans-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/
```

The PyPI package is `belindoc-mcp`; the repository directory and Python module are still
called `trans-mcp` / `trans_mcp`, and the `trans-mcp` / `trans-mcp-http` commands are kept for
existing deployment scripts.

Release notes for maintainers (publishing to PyPI, project layout) are in
[README.zh-CN.md](README.zh-CN.md#开发).

## License

MIT License
