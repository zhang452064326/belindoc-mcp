# Installing belindoc-mcp (guide for AI agents)

This file is for AI assistants (Cline, Claude Code, Cursor, …) setting up this server for a
user. Humans can read [README.md](README.md) instead.

belindoc-mcp is published on PyPI. **Do not clone this repository or build it** — run the
published package with `uvx`.

## 1. Get the user's API key

The server needs a Belindoc API key. Ask the user for it; never invent one.

- Format: `ft_` followed by 40 characters (43 characters in total).
- The user creates it at https://belindoc.com → Open Platform (开放平台) → API Keys
  (API Key 管理). Registration is self-service.

Put the key only in the MCP settings `env` block shown below. Do not write it into any file
in the user's project.

## 2. Make sure `uvx` is available

```bash
uvx --version
```

If the command is missing, install uv:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

uv is only on `PATH` in shells opened after the install. MCP clients usually don't read the
login shell's `PATH` either, so if the server later fails with "uvx not found", use the
absolute path from `which uvx` (for example `~/.local/bin/uvx`) as `command`.

## 3. Add the server to the MCP settings

```json
{
  "mcpServers": {
    "belindoc-mcp": {
      "command": "uvx",
      "args": ["belindoc-mcp"],
      "env": {
        "BELINDOC_API_KEY": "ft_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

Replace the placeholder with the user's key. Nothing else is required.

Optional `env` entries:

- `MCP_LOCALE` — language of status and progress messages shown to the user. Default `zh`;
  one of `zh`, `zh-Hant`, `en`, `ja`, `ko`, `de`, `fr`, `ru`, `ar`. Set `en` for an
  English-speaking user.
- `BELINDOC_API_BASE_URL` — leave it unset. The default is production
  (`https://belindoc.com/api`).

### Alternative: hosted endpoint (no local install)

If the client supports remote Streamable HTTP servers with custom headers, it can connect
without uv:

```json
{
  "mcpServers": {
    "belindoc-mcp": {
      "type": "streamableHttp",
      "url": "https://mcp.belindoc.com/api/mcp",
      "headers": {
        "Authorization": "Bearer ft_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

The value of `type` differs between clients (`streamableHttp` in Cline, `http` in Claude
Code); check the client's documentation.

## 4. Verify

Call the `get_account_status` tool. It costs nothing and returns the account's balance. If it
reports an invalid key, ask the user to check the key; don't retry with a guessed one.

## Notes for using the tools

- Translation and video jobs consume the user's paid quota. The server asks the user to
  confirm before submitting a paid job; never confirm on the user's behalf.
- Uploads work in two steps: `upload_document` / `upload_video` return an `uploadCommand`,
  which you run to upload the local file.
- Download links are signed URLs. Pass them to the user unchanged; a shortened or edited link
  returns 403.
