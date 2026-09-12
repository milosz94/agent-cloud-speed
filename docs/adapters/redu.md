# redu adapter

## You must do (not automatable)

1. **A redu account**, and a login token the agent can use.

## The repo does

- ships `config/redu.mcp.json` and copies it into place (`setup.sh`); it points at the hosted MCP
  over HTTP, so there is no CLI to install and nothing in the file to edit

## Known shape of this adapter

The MCP server is HTTP rather than a local process, so the microVM carries only the redu login token
and nothing else. Deploy and teardown are both MCP tool calls; no vendor CLI is involved.
