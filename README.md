# validation-mcp-server

An MCP server that exposes the [validation-lib](https://github.com/judepayne/validation-lib) rule-based data validation library to LLM clients such as Claude Desktop.

## Setup

Clone the repo and install dependencies:

```bash
git clone https://github.com/judepayne/validation-mcp-server.git
cd validation-mcp-server
pip install -r requirements.txt
```

## Claude Desktop integration

Add the following to your Claude Desktop config file:

```json
{
  "mcpServers": {
    "validation-lib": {
      "command": "python",
      "args": ["/path/to/validation-mcp-server/server.py"]
    }
  }
}
```

The config file is found at:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json` (create if absent)
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Note: on some macOS installations the file may simply be named `config.json` in the same directory.

Replace `/path/to/validation-mcp-server/server.py` with the actual path on your machine, then restart Claude Desktop.

## Tools

The server exposes 26 tools:

| Tool | Description |
|------|-------------|
| `discover_rulesets` | List all available rulesets |
| `discover_rules` | List all rules in a ruleset |
| `example_loan` | Return a valid example loan record for exploration |
| `generate_loan` | Instructions for the LLM to generate a realistic test loan grounded in the actual schemas and rules |
| `validate` | Validate a single data record against a ruleset |
| `batch_validate` | Validate a list of records against a ruleset |
| `batch_file_validate` | Validate records from a JSON/CSV file |
| `load_logic` | Load rule logic from GitHub (skips if already cached) |
| `reload_logic` | Force a fresh fetch of rule logic from GitHub |
| `get_cache_age` | Check how old the local rule cache is |
| `list_logic_files` | Browse cached rule files as a directory tree |
| `read_logic_file` | Read the source of a specific rule file |
| `refresh_inbox` | Top up the inbox to 4 loans — call only when the user explicitly requests a refresh |
| `get_workflow` | Return full workflow state across all four folders with file timestamps; always displayed as a table |
| `next_loan_number` | Return the next available loan number and pre-formatted filename |
| `list_workflow` | List files in one workflow folder or all four |
| `read_workflow_file` | Read a loan file from the workflow directory |
| `write_workflow_file` | Write content to a file in the workflow directory |
| `move_workflow_file` | Move a loan file between workflow folders |
| `delete_workflow_file` | Permanently delete a single loan file from a workflow folder |
| `clear_workflow_folder` | Permanently delete all files in a workflow folder |
| `validate_loan_file` | Validate a workflow loan file and append a validation note |
| `batch_validate_loan_files` | Validate multiple workflow loan files and append a validation note to each |
| `add_note` | Append a manual note entry to a workflow loan file |
| `edit_loan_file` | Edit loan fields via dot-notation and record an audit note |
| `show_status` | Render a live auto-refreshing workflow status panel (MCP App) |
