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

The server exposes 27 tools, grouped by concern:

### Rule discovery

| Tool | Description |
|------|-------------|
| `discover_rulesets` | List all available rulesets |
| `discover_rules(entity_type, schema_url, ruleset_name)` | List rules and metadata for a ruleset — pass the schema URL directly |

### Validation

| Tool | Description |
|------|-------------|
| `validate` | Validate a single entity against a ruleset |
| `batch_validate` | Validate a list of entities against a ruleset |
| `validation_report(ruleset_name, folder=None, relative_paths=None)` | Dry-run aggregate report — no notes written; pass a folder name for convenience or an explicit path list; returns pass/fail/warn counts and a per-rule failure breakdown |

### Logic cache

| Tool | Description |
|------|-------------|
| `reload_logic` | Force a fresh fetch of rule logic from GitHub |
| `get_cache_age` | Check how old the local rule cache is |
| `list_logic_files` | Browse cached rule files as a directory tree |
| `read_logic_file` | Read the source of a specific rule file |

> **Cache freshness**: At startup, `ValidationService` automatically reloads rules from GitHub if the local cache is older than `logic_cache_max_age_seconds` (default 1800 s / 30 min, configured in `local-config.yaml`). The same limit is applied every 5 minutes during a session. You can also trigger an immediate refresh at any time via `reload_logic()`.

### Loan data

| Tool | Description |
|------|-------------|
| `generate_loan` | Instructions for the LLM to generate a realistic test loan grounded in the actual schemas and rules |

### Entity conversion

Converts between the **physical** representation (nested JSON as stored on disk, with versioned field names) and the **logical** representation (flat dict with stable logical field names and typed values). The full field mapping for each schema version is in `entity_helpers/loan_v*.json` — browse with `list_logic_files()`.

| Tool | Description |
|------|-------------|
| `convert_to_logical` | Convert a physical loan dict to a flat logical dict; schema auto-detected from `$schema` |
| `convert_to_physical` | Convert a flat logical dict back to a nested physical dict; schema name supplied explicitly |

### Help

| Tool | Description |
|------|-------------|
| `list_commands` | Return a formatted reference of all available commands grouped by category, with examples |

### Workflow — file operations

| Tool | Description |
|------|-------------|
| `refresh_inbox` | Top up the inbox to 4 loans, generating varied records server-side — call only when the user explicitly requests a refresh |
| `full_workflow_summary(folder=None)` | Table view of workflow state with timestamps and principal amounts; triggered by any workflow/summary request without 'quick' or 'short' |
| `quick_workflow_summary()` | Live auto-refreshing counts panel (MCP App); triggered only when the user says 'quick' or 'short' |
| `search_workflow(field_path, value, folders=None)` | Find loans matching a field value across folders (e.g. `search_workflow('financial.currency', 'USD')`) |
| `read_workflow_files(relative_paths=None)` | Read specific loan files by path, or omit to read all files across all folders in one call |
| `write_workflow_file` | Write content to a file in the workflow directory |
| `move_workflow_files(filenames, from_folder, to_folder)` | Move one or more loan files between folders in a single call |
| `delete_workflow_files(relative_paths)` | Permanently delete one or more loan files; paths may span different folders |
| `clear_workflow_folder` | Permanently delete all files in a workflow folder |

### Workflow — validation

| Tool | Description |
|------|-------------|
| `batch_validate_loan_files` | Validate one or more specific workflow loan files and append a validation note to each (pass a single-element list for one file) |
| `batch_validate_inbox` | Validate every file currently in the inbox and append a validation note to each |

### Workflow — editing & notes

| Tool | Description |
|------|-------------|
| `edit_loan_file` | Edit loan fields via dot-notation and record an audit note |
| `add_note` | Append a freeform note to a workflow loan file |
| `get_notes` | Return just the audit trail (notes array) from a workflow loan file |


