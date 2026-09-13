# Azure adapter

## You must do (not automatable)

1. **An Azure subscription** you can create and delete resource groups in.
2. **Authenticate:**
   ```bash
   az login
   ```
   For an unattended batch use a service principal instead, exporting `AZURE_TENANT_ID`,
   `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET`.

## The repo does

- writes `~/.acspeed/_config/azure.mcp.json` on first use (`autorun.py::ensure_adapter_config`);
  nothing in it needs editing
- checks `az account show` and prints the fix when it fails
  (`setup.sh --check --adapter azure`), the same check the run itself makes

## Known shape of this adapter

The MCP server is `@azure/mcp` (`azmcp`). Its native compute and app-service tools are mostly
read/query, so the create, deploy and teardown path runs through the `extension` namespace driving
`az` / `azd`: `az containerapp up`, `az webapp up`, `az vm create`, and `az group delete` to tear
down. Cost for a Container Apps deploy is a per-usage schedule read from the public Azure Retail
Prices API, which needs no credentials.
