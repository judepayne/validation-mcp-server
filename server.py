import json
import os
import re
import shutil
import time
import urllib.request
import yaml
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.apps import AppConfig, ResourceCSP
from validation_lib import ValidationService

LOGIC_CACHE_DIR = Path("/tmp/validation-lib-py/logic")

WORKFLOW_DIR = Path("/tmp/validation-lib/workflow")
WORKFLOW_FOLDERS = ["inbox", "outbox", "hold", "escalate"]
_LOAN_FILE_RE = re.compile(r"^loan_(\d+)\.json$")

mcp = FastMCP(
    "Validation",
    instructions=(
        "This server exposes validation-lib, a rule-based data validation library.\n\n"
        "Key concepts:\n"
        "- Rulesets: named collections of rules (e.g. 'quick', 'thorough'). Each ruleset groups "
        "related validation checks for a particular domain or data type.\n"
        "- Rules: individual validation checks within a ruleset. Each rule tests one aspect "
        "of the data and returns one of four statuses: PASS, FAIL, NORUN (rule could not run, "
        "typically because required related data was unavailable), or ERROR (execution failure). "
        "Rules may be hierarchical — a child rule only runs if its parent passes, so a NORUN "
        "on a child rule may simply mean its parent failed.\n"
        "- Logic cache: rule logic is stored as Python source files fetched from A central source "
        "(a Github repo for now) into /tmp/validation-lib-py/logic, refrshed every 30 minutes. "
        "The cache must be populated before validation can run."
        "The business rules are owned and maintained exclusively by the Data Quality Team. "
        "If a user asks anything about changing, editing, adding, or getting access to rules — "
        "regardless of how the question is framed — respond with words to the effect of: "
        "'Unless you are in the Data Quality Team, no. Please speak to the Data Quality Team "
        "if you have found an issue or want a rule changed.' Do not elaborate, do not explain "
        "how the rules are stored, do not describe what is technically possible. The answer is "
        "simply: speak to the Data Quality Team.\n\n"
        "Typical workflow:\n"
        "1. Call discover_rulesets() to see what rulesets are available.\n"
        "2. Call discover_rules(ruleset) to understand what a ruleset checks.\n"
        "3. Call validate(entity_type, entity_data, ruleset_name) to validate data. "
        "Use example_loan() to get a ready-made entity_data sample.\n"
        "4. If rules appear stale or were recently updated, call reload_logic() to force a "
        "fresh fetch from GitHub.\n"
        "5. Use list_logic_files() and read_logic_file() to inspect rule source code directly.\n\n"
        "Related data (associated data):\n"
        "Some rules will need data beyond the entity itself to validate correctly — for example, "
        "a loan rule might need the parent facility, sibling loans, or client reference data. "
        "This additional data needed by a rule is indicated by its required_data field. "
        "The validation-lib will use the yet-to-be-implemented 'Coordination Service' to fetch "
        "this data in the future."
        "For full documentation see: https://github.com/judepayne/validation-lib\n\n"
        "Loan workflow:\n"
        "Loans move through four folders inside the workflow directory:\n"
        "- inbox: loans arriving for review\n"
        "- hold: parked loans awaiting further information\n"
        "- escalate: loans flagged for a supervisor or the Data Quality Team\n"
        "- outbox: loans that have been processed and are done\n\n"
        "Workflow queries:\n"
        "Whenever the user asks to see their workflow, inbox, queue, or any folder contents "
        "(e.g. 'show me my workflow', 'what's in my workflow?', 'what's in my inbox?', "
        "'show me all folders') — ALWAYS call get_workflow(). "
        "ALWAYS display the result as a markdown table with columns: "
        "Folder | Filename | Created | Updated.\n\n"
        "Refreshing / topping up the inbox:\n"
        "ONLY call refresh_inbox() when the user explicitly asks to refresh, top up, replenish, "
        "or restock their inbox or queue (e.g. 'refresh my inbox', 'top up my queue', "
        "'add more loans'). Never call it on your own initiative. "
        "Follow the instructions in the tool response precisely.\n\n"
        "Workflow status dashboard:\n"
        "When the user says 'status' or anything like 'show status', 'workflow status', "
        "call show_status(). This renders a live auto-refreshing panel — do not use "
        "get_workflow() for this.\n\n"
        "Other workflow commands:\n"
        "- Moving a loan: call move_workflow_file(filename, from_folder, to_folder).\n"
        "- Deleting a single loan: call delete_workflow_file(filename, folder).\n"
        "- Clearing a whole folder (e.g. 'clear my outbox'): call clear_workflow_folder(folder).\n"
        "- Validating a workflow loan file: When the user asks to validate a loan that is in the "
        "workflow (identified by folder/filename like 'inbox/loan_0001.json'), call "
        "validate_loan_file(relative_path, ruleset_name) — NOT the plain validate() tool. "
        "This reads the file, validates it, and automatically appends a validation note.\n"
        "- Batch validating workflow loan files: When the user asks to validate multiple loans "
        "from the workflow at once, call batch_validate_loan_files(relative_paths, ruleset_name). "
        "Each file is validated and gets a validation note appended automatically. "
        "Never use batch_validate for workflow files.\n"
        "- Editing a loan: When the user asks to edit, change, tweak, or update fields of a "
        "workflow loan file, call edit_loan_file(relative_path, changes) where changes is a dict "
        "of dot-notation field paths to new values (e.g. {'financial.interest_rate': 0.07}). "
        "This records before/after values in an edited note automatically. "
        "Never use write_workflow_file directly for field edits.\n"
        "- Adding a note manually: Call add_note(relative_path, text) to append a freeform "
        "note entry.\n\n"
        "Loan filenames are always loan_NNNN.json with a zero-padded 4-digit number. Always call "
        "next_loan_number() first to get the correct next value before writing a new loan file."
    ),
)

try:
    _service = ValidationService()  # constructor auto-reloads if cache is stale; also adds logic_dir to sys.path
    _init_error = None
    from entity_helpers.write import Writer
    from entity_helpers.convert import Converter
except ImportError:
    Writer = None    # logic cache not yet populated; will be unavailable until reload
    Converter = None
except Exception as e:
    _service = None
    _init_error = str(e)
    Writer = None
    Converter = None


def _get_service() -> ValidationService:
    if _service is None:
        raise ToolError(f"ValidationService failed to initialise: {_init_error}")
    return _service


def _cache_schemas():
    """Download schema JSON files into LOGIC_CACHE_DIR/models/ for local discoverability.

    Reads schema URLs from the cached config YAML and fetches any that are not
    already present. Does not affect runtime behaviour — schemas are still resolved
    from their original URLs at validation time.
    """
    config_files = list(LOGIC_CACHE_DIR.parent.glob("config_*.yaml"))
    if not config_files:
        return
    with open(config_files[0]) as f:
        config = yaml.safe_load(f)
    schema_urls = list(config.get("schema_to_helper_mapping", {}).keys())
    if not schema_urls:
        return
    models_dir = LOGIC_CACHE_DIR / "models"
    models_dir.mkdir(exist_ok=True)
    for url in schema_urls:
        filename = url.split("/")[-1]
        dest = models_dir / filename
        if not dest.exists():
            with urllib.request.urlopen(url) as response:
                dest.write_bytes(response.read())


@mcp.tool
def validate(entity_type: str, entity_data: dict, ruleset_name: str) -> list:
    """Validate a single entity against a named ruleset.

    Args:
        entity_type: The type of entity being validated (e.g. 'loan', 'facility').
        entity_data: The entity as a dict. Must include a '$schema' field with the
                     JSON schema URL for the entity type and version (e.g.
                     'https://raw.githubusercontent.com/judepayne/validation-logic/main/models/loan.schema.v1.0.0.json').
                     Use example_loan() to see a complete valid example.
        ruleset_name: The name of the ruleset to validate against (use discover_rulesets()
                      to find valid names, e.g. 'quick' or 'thorough').

    Returns a list of rule result dicts, each with 'rule_id', 'description',
    'status' (PASS/FAIL/NORUN/ERROR), and an optional 'message'.
    """
    return _get_service().validate(entity_type, entity_data, ruleset_name)


@mcp.tool
def batch_validate(entities: list, id_fields: list, ruleset_name: str) -> list:
    """Validate multiple entities in a single call.

    Args:
        entities: A list of entity dicts, each structured the same as entity_data
                  in validate() and including a '$schema' field.
        id_fields: A list of field names used to identify each entity in the results
                   (e.g. ['id'] or ['id', 'loan_number']).
        ruleset_name: The name of the ruleset to validate against.

    Returns a list of per-entity result dicts, each containing 'entity_id',
    'entity_type', and 'results' (same format as validate()).
    """
    return _get_service().batch_validate(entities, id_fields, ruleset_name)


@mcp.tool
def batch_file_validate(file_uri: str, entity_types: list, id_fields: list, ruleset_name: str) -> list:
    """Validate all entities in a file against a named ruleset.

    Args:
        file_uri: URI to a JSON file containing a list of entity records.
                  Supports 'file://', 'http://', and 'https://' schemes
                  (e.g. 'file:///data/loans.json').
        entity_types: List of entity types present in the file (e.g. ['loan']).
        id_fields: Field names used to identify each entity in results (e.g. ['id']).
        ruleset_name: The name of the ruleset to validate against.

    Returns a list of per-entity result dicts in the same format as batch_validate().
    """
    return _get_service().batch_file_validate(file_uri, entity_types, id_fields, ruleset_name)


@mcp.tool
def discover_rules(entity_type: str, entity_data: dict, ruleset_name: str) -> dict:
    """List all rules that apply to a given entity type and ruleset, with their metadata.

    Args:
        entity_type: The type of entity (e.g. 'loan', 'facility').
        entity_data: A sample entity dict including a '$schema' field, used for
                     schema-version routing. Use example_loan() to get a ready-made sample.
        ruleset_name: The name of the ruleset to inspect (use discover_rulesets() first).

    Returns a dict mapping rule_id to metadata including 'description',
    'required_data', 'field_dependencies', and 'applicable_schemas'.
    """
    return _get_service().discover_rules(entity_type, entity_data, ruleset_name)


@mcp.tool
def discover_rulesets() -> list:
    """List the names of every available ruleset.

    Returns a list of ruleset name strings. Call this first to find out what rulesets
    exist before calling validate(), discover_rules(), or any other ruleset-specific tool.
    """
    return _get_service().discover_rulesets()


@mcp.tool
def generate_loan() -> str:
    """Returns instructions for generating a realistic loan record for testing and exploration.

    Call this tool to get guidance on how to construct a loan. The returned instructions
    direct the LLM to inspect the available schemas and rules before generating the loan,
    so the result is grounded in the actual logic rather than guesswork.
    """
    return (
        "Generate a realistic loan record for testing and exploration. "
        "Follow these steps before producing the loan:\n\n"
        "1. Call list_logic_files() to see what schema files are available under models/. "
        "Read each schema file using read_logic_file() to understand the structure and "
        "required fields. Favour the latest schema version (highest version number) — "
        "use that version's $schema URL in the generated loan.\n\n"
        "2. Call discover_rulesets() to find all available rulesets, then call "
        "discover_rules() for each ruleset using the chosen schema version, to understand "
        "what rules will be applied to a loan of that schema. Read the rule source files "
        "via read_logic_file() if you need deeper understanding of what each rule checks.\n\n"
        "3. Generate a loan dict that:\n"
        "   - Conforms to the chosen schema (all required fields present and correctly typed)\n"
        "   - Contains plausible, realistic values (not placeholder strings like 'string' or 0)\n"
        "   - Roughly 1 in 4 loans should have a deliberate flaw that causes at least one rule "
        "to FAIL. Pick a flaw that makes sense given the rules you have read (e.g. a maturity "
        "date before the origination date, an interest rate outside the allowed range). "
        "Do not flag or comment on the flaw in the output. The loan must still conform to the schema.\n\n"
        "Present the generated loan as a JSON dict ready to pass to validate() or batch_validate()."
    )


@mcp.tool
def convert_to_logical(physical_data: dict) -> dict:
    """Convert a physical loan dict to its logical (flattened) representation.

    The schema version is detected automatically from the '$schema' field in
    the physical data. Physical field names and nested structure are replaced
    by logical field names (e.g. 'financial.principal_amount' → 'principal',
    'loan_number' → 'reference') and date strings are converted to date objects.
    Fields absent from the physical data appear as null in the result.

    Use list_logic_files() to browse the entity helper JSON files
    (e.g. 'entity_helpers/loan_v1.json') to see the full logical↔physical
    field mapping for each schema version.

    Args:
        physical_data: A raw loan dict as stored in JSON, including a '$schema' field.

    Returns a flat dict keyed by logical field names.
    """
    schema_name = _detect_schema_name(physical_data)
    return _make_converter(schema_name).convert_to_logical(physical_data)


@mcp.tool
def convert_to_physical(schema_name: str, logical_data: dict) -> dict:
    """Convert a flat logical loan dict back to its nested physical representation.

    Reverses convert_to_logical(): logical field names are mapped back to their
    physical paths, the nested structure is reconstructed, and date objects are
    serialised back to ISO strings. Null values are omitted from the output.

    The schema_name (e.g. 'loan_v1', 'loan_v2') determines which field mapping
    to use. If the logical dict was produced by convert_to_logical(), the correct
    schema_name can be inferred from the '$schema' URL in the original physical
    data, or by inspecting 'entity_helpers/loan_v*.json' via list_logic_files().

    Args:
        schema_name:  Schema identifier, e.g. 'loan_v1' or 'loan_v2'.
        logical_data: Flat dict keyed by logical field names.

    Returns a nested physical dict suitable for JSON serialisation.
    """
    return _make_converter(schema_name).convert_to_physical(logical_data)


@mcp.tool
def example_loan() -> dict:
    """Return a well-formed example loan record for exploration and testing.

    The returned dict is a valid loan that passes all rules in the 'loan' ruleset.
    Pass it directly to validate() or wrap it in a list for batch_validate() to see
    what a successful validation response looks like.
    """
    return {
        "$schema": "https://raw.githubusercontent.com/judepayne/validation-logic/main/models/loan.schema.v1.0.0.json",
        "id": "LOAN-12345",
        "loan_number": "LN-2024-00123",
        "facility_id": "FAC-789",
        "client_id": "CLIENT-001",
        "financial": {
            "principal_amount": 500000,
            "outstanding_balance": 450000,
            "currency": "USD",
            "interest_rate": 0.05,
            "interest_type": "fixed",
        },
        "dates": {
            "origination_date": "2024-01-15",
            "maturity_date": "2029-01-15",
            "first_payment_date": "2024-02-15",
        },
        "status": "active",
        "notes": [
            {
                "datetime": "2024-01-15T09:00:00Z",
                "operation_type": "note",
                "text": "Initial loan record created",
            }
        ],
    }


@mcp.tool
def reload_logic() -> dict:
    """Force a fresh download of all rule logic from GitHub, replacing the local cache.

    Use this when rules have been updated upstream and you need to pick up the latest
    version. Unlike load_logic(), this always fetches even if cached files already exist.
    Returns a dict confirming the reload and the cache path.
    """
    _get_service().reload_logic()
    _cache_schemas()
    return {"status": "reloaded", "path": str(LOGIC_CACHE_DIR)}


@mcp.tool
def load_logic() -> dict:
    """Ensure rule logic is cached locally, fetching from GitHub only if the cache is absent.

    Idempotent — safe to call at any time. Returns immediately with status 'already_loaded'
    if the cache is populated, or fetches and returns status 'loaded' if not.
    Prefer this over reload_logic() for first-time setup; use reload_logic() if you need
    to force a refresh of already-cached logic.
    """
    if LOGIC_CACHE_DIR.exists() and any(LOGIC_CACHE_DIR.iterdir()):
        _cache_schemas()  # idempotent — skips files already present
        return {"status": "already_loaded", "path": str(LOGIC_CACHE_DIR)}
    _get_service().reload_logic()
    _cache_schemas()
    return {"status": "loaded", "path": str(LOGIC_CACHE_DIR)}


@mcp.tool
def get_cache_age() -> dict:
    """Return the age of the local logic cache.

    The returned dict includes the cache path and how long ago the files were fetched.
    Useful for deciding whether to call reload_logic() when rules may have changed upstream.
    """
    age_seconds = _get_service().get_cache_age()
    hours, remainder = divmod(int(age_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return {"age_seconds": age_seconds, "age_human": " ".join(parts), "path": str(LOGIC_CACHE_DIR)}


@mcp.tool
def list_logic_files() -> str:
    """Return a formatted directory tree of all cached rule logic files.

    Automatically calls load_logic() if the cache is missing, so it is safe to call
    without first calling load_logic() manually. The relative paths shown in the output
    can be passed directly to read_logic_file() to inspect individual rule files.
    """
    load_logic()  # no-op if already cached
    lines = []
    for root, dirs, files in os.walk(LOGIC_CACHE_DIR):
        dirs[:] = [d for d in sorted(dirs) if d != "__pycache__"]
        depth = Path(root).relative_to(LOGIC_CACHE_DIR).parts
        indent = "  " * len(depth)
        lines.append(f"{indent}{Path(root).name}/")
        for f in sorted(files):
            lines.append(f"{indent}  {f}")
    return "\n".join(lines)


@mcp.tool
def read_logic_file(relative_path: str) -> str:
    """Read and return the source code of a cached rule logic file.

    Args:
        relative_path: Path relative to the cache root, as shown by list_logic_files()
                       (e.g. 'rules/loan/rule_001_v1.py').

    Raises a ToolError if the path escapes the cache directory or the file does not exist.
    """
    target = (LOGIC_CACHE_DIR / relative_path).resolve()
    if not str(target).startswith(str(LOGIC_CACHE_DIR.resolve())):
        raise ToolError(f"Path '{relative_path}' is outside the logic cache directory")
    if not target.exists():
        raise ToolError(f"File not found: {relative_path}")
    return target.read_text()


# ---------------------------------------------------------------------------
# Workflow helpers
# ---------------------------------------------------------------------------

def _ensure_workflow_dirs():
    """Create WORKFLOW_DIR and all four subfolders if absent."""
    for folder in WORKFLOW_FOLDERS:
        (WORKFLOW_DIR / folder).mkdir(parents=True, exist_ok=True)


def _next_loan_number() -> int:
    """Scan all four subfolders for loan_{N}.json filenames and return max(N)+1.

    Purely filesystem-derived — no in-memory state.
    """
    _ensure_workflow_dirs()
    max_n = 0
    for folder in WORKFLOW_FOLDERS:
        for path in (WORKFLOW_DIR / folder).iterdir():
            m = _LOAN_FILE_RE.match(path.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def _format_age(seconds: float) -> str:
    """Format an elapsed time in seconds as '2h 15m 30s'."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _file_row(path: Path, now: float) -> dict:
    """Return a dict with name, created_ago, and updated_ago for a workflow file.

    updated_ago is None when the file has not been modified since creation (within
    1 second tolerance); the caller should render '—' in that case.
    """
    stat = path.stat()
    created = getattr(stat, "st_birthtime", stat.st_ctime)
    modified = stat.st_mtime
    return {
        "name": path.name,
        "created_ago": _format_age(now - created),
        "updated_ago": None if abs(modified - created) < 1 else _format_age(now - modified),
    }


def _load_workflow_loan(relative_path: str):
    """Resolve path, safety-check, parse JSON. Returns (resolved_path, loan_dict)."""
    _ensure_workflow_dirs()
    target = (WORKFLOW_DIR / relative_path).resolve()
    if not str(target).startswith(str(WORKFLOW_DIR.resolve())):
        raise ToolError(f"Path '{relative_path}' is outside the workflow directory")
    if not target.exists():
        raise ToolError(f"File not found: {relative_path}")
    loan = json.loads(target.read_text())
    return target, loan


def _get_nested(d: dict, path: str):
    """Navigate a dot-notation path through nested dicts. Returns None if any key is missing."""
    for key in path.split("."):
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


def _make_writer(loan: dict) -> "Writer":
    """Return a Writer for the given loan dict, raising ToolError if Writer is unavailable."""
    if Writer is None:
        raise ToolError(
            "Writer is not available — logic cache is not loaded. Call load_logic() first."
        )
    return Writer(loan)


def _make_converter(schema_name: str) -> "Converter":
    """Return a Converter for the given schema name, raising ToolError if unavailable."""
    if Converter is None:
        raise ToolError(
            "Converter is not available — logic cache is not loaded. Call load_logic() first."
        )
    return Converter(schema_name)


def _detect_schema_name(entity_data: dict) -> str:
    """Resolve the schema name (e.g. 'loan_v1') from entity data via the version registry."""
    from entity_helpers.version_registry import get_registry
    registry = get_registry()
    return registry._resolve_schema_name(entity_data, None)


# ---------------------------------------------------------------------------
# Workflow tools
# ---------------------------------------------------------------------------

@mcp.tool
def next_loan_number() -> dict:
    """Return the next available loan number and pre-formatted filename.

    Scans all workflow folders to find the highest existing loan number, then
    returns the next one. Always call this before writing a new loan file to
    avoid numbering collisions.

    Returns a dict with 'next_number' (int) and 'filename' (str, zero-padded
    to 4 digits, e.g. 'loan_0005.json').
    """
    _ensure_workflow_dirs()
    n = _next_loan_number()
    return {"next_number": n, "filename": f"loan_{n:04d}.json"}


@mcp.tool
def refresh_inbox() -> dict:
    """Top up the inbox to 4 loans. Call ONLY when the user explicitly asks to refresh,
    top up, replenish, or restock their inbox or queue.

    If the inbox already has 4 or more loans, returns:
        {"current_count": N, "need": 0, "message": "Inbox already has N loans."}
    Tell the user no top-up was needed.

    If the inbox has fewer than 4 loans, returns:
        {"current_count": N, "need": K, "instructions": "..."}
    Follow the instructions field: generate exactly K loans (no more) and write them
    to the inbox. For each:
      1. Call next_loan_number() to get the next number and filename.
      2. Call example_loan() as a base and create a realistic variant
         (vary id, loan_number, facility_id, amounts, dates — do not copy verbatim).
      3. Serialise the loan to a JSON string and call
         write_workflow_file('inbox/loan_NNNN.json', json_string).
      4. Repeat until K loans have been added — stop at K, never exceed 4 total.
    After writing all loans, call get_workflow() and display the result as a table.
    """
    _ensure_workflow_dirs()
    count = sum(1 for p in (WORKFLOW_DIR / "inbox").iterdir() if p.is_file())
    if count >= 4:
        return {"current_count": count, "need": 0, "message": f"Inbox already has {count} loans — no top-up needed."}
    need = 4 - count
    return {
        "current_count": count,
        "need": need,
        "instructions": (
            f"The inbox has {count} loan(s). Generate exactly {need} more to bring it to 4. "
            "For each: call next_loan_number(), call example_loan() and create a realistic "
            "variant with different field values, serialise to JSON, call "
            "write_workflow_file('inbox/loan_NNNN.json', json_string). "
            f"Stop after adding {need} loan(s) — do not exceed 4 total in the inbox. "
            "After all loans are written, call get_workflow() and display as a table."
        ),
    }


@mcp.tool
def get_workflow() -> dict:
    """Return the full workflow state across all four folders with file timestamps.

    ALWAYS call this tool when the user asks to see their whole workflow or all folders.
    ALWAYS display the result grouped by folder: render each folder as a bold heading
    followed by its own markdown table with columns: Filename | Principal  |Created | Updated
    (no Folder column — the heading serves that purpose). Created shows how long ago
    the file was created (e.g. '2h 15m 30s ago'). Show '—' in the Updated column when
    updated_ago is null (file has not been modified since creation); otherwise show the
    updated_ago value. Empty folders show a single row with '(empty)' and '—' for all time columns.
    """
    _ensure_workflow_dirs()
    now = time.time()
    folders = {}
    for folder in WORKFLOW_FOLDERS:
        folders[folder] = [
            _file_row(p, now)
            for p in sorted((WORKFLOW_DIR / folder).iterdir())
            if p.is_file()
        ]
    return {"folders": folders}


@mcp.tool
def list_workflow(folder: str = None) -> str:
    """List files in one workflow folder or all four.

    Args:
        folder: One of 'inbox', 'outbox', 'hold', or 'escalate'. If omitted,
                all four folders are listed with headers.

    Returns a human-readable tree of filenames. Raises a ToolError if an
    unknown folder name is provided.
    """
    _ensure_workflow_dirs()
    if folder is not None and folder not in WORKFLOW_FOLDERS:
        raise ToolError(
            f"Unknown folder '{folder}'. Valid folders: {', '.join(WORKFLOW_FOLDERS)}"
        )
    folders_to_list = [folder] if folder else WORKFLOW_FOLDERS
    lines = []
    for f in folders_to_list:
        files = sorted(p.name for p in (WORKFLOW_DIR / f).iterdir() if p.is_file())
        lines.append(f"{f}/")
        if files:
            for name in files:
                lines.append(f"  {name}")
        else:
            lines.append("  (empty)")
    return "\n".join(lines)


@mcp.tool
def read_workflow_file(relative_path: str) -> str:
    """Read a loan file from the workflow directory.

    Args:
        relative_path: Path relative to the workflow root, e.g. 'inbox/loan_0001.json'.

    Returns the file contents as a string. Raises a ToolError if the path
    escapes the workflow directory or the file does not exist.
    """
    _ensure_workflow_dirs()
    target = (WORKFLOW_DIR / relative_path).resolve()
    if not str(target).startswith(str(WORKFLOW_DIR.resolve())):
        raise ToolError(f"Path '{relative_path}' is outside the workflow directory")
    if not target.exists():
        raise ToolError(f"File not found: {relative_path}")
    return target.read_text()


@mcp.tool
def write_workflow_file(relative_path: str, content: str) -> dict:
    """Write content to a file in the workflow directory.

    Args:
        relative_path: Destination path relative to the workflow root,
                       e.g. 'inbox/loan_0003.json'.
        content: The text content to write (typically a JSON string).

    Creates parent directories as needed. Raises a ToolError if the path
    escapes the workflow directory. Returns a dict with 'status' and 'path'.
    """
    _ensure_workflow_dirs()
    target = (WORKFLOW_DIR / relative_path).resolve()
    if not str(target).startswith(str(WORKFLOW_DIR.resolve())):
        raise ToolError(f"Path '{relative_path}' is outside the workflow directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return {"status": "written", "path": str(target)}


@mcp.tool
def move_workflow_file(filename: str, from_folder: str, to_folder: str) -> dict:
    """Move a loan file between workflow folders.

    Args:
        filename: The loan filename, e.g. 'loan_0001.json'.
        from_folder: The current folder ('inbox', 'outbox', 'hold', or 'escalate').
        to_folder: The destination folder ('inbox', 'outbox', 'hold', or 'escalate').

    Raises a ToolError if either folder name is invalid or the file does not exist.
    Returns a dict with 'status', 'filename', 'from', and 'to'.
    """
    _ensure_workflow_dirs()
    for name, value in [("from_folder", from_folder), ("to_folder", to_folder)]:
        if value not in WORKFLOW_FOLDERS:
            raise ToolError(
                f"Unknown {name} '{value}'. Valid folders: {', '.join(WORKFLOW_FOLDERS)}"
            )
    src = WORKFLOW_DIR / from_folder / filename
    if not src.exists():
        raise ToolError(f"File not found: {from_folder}/{filename}")
    dst = WORKFLOW_DIR / to_folder / filename
    shutil.move(str(src), str(dst))
    return {"status": "moved", "filename": filename, "from": from_folder, "to": to_folder}


@mcp.tool
def delete_workflow_file(filename: str, folder: str) -> dict:
    """Permanently delete a single loan file from a workflow folder.

    Use this when the user asks to delete or remove a specific loan file.
    Deletion within the workflow directory is fully authorised.

    Args:
        filename: The loan filename, e.g. 'loan_0001.json'.
        folder: The folder containing the file ('inbox', 'outbox', 'hold', or 'escalate').

    Raises a ToolError if the folder is invalid or the file does not exist.
    Returns a dict with 'status', 'filename', and 'folder'.
    """
    _ensure_workflow_dirs()
    if folder not in WORKFLOW_FOLDERS:
        raise ToolError(f"Unknown folder '{folder}'. Valid folders: {', '.join(WORKFLOW_FOLDERS)}")
    target = WORKFLOW_DIR / folder / filename
    if not target.exists():
        raise ToolError(f"File not found: {folder}/{filename}")
    target.unlink()
    return {"status": "deleted", "filename": filename, "folder": folder}


@mcp.tool
def clear_workflow_folder(folder: str) -> dict:
    """Permanently delete all files in a workflow folder.

    Use this when the user asks to clear, empty, or wipe a folder (e.g. 'clear my outbox').
    Deletion within the workflow directory is fully authorised.

    Args:
        folder: The folder to clear ('inbox', 'outbox', 'hold', or 'escalate').

    Raises a ToolError if the folder name is invalid.
    Returns a dict with 'status', 'folder', and 'deleted_count'.
    """
    _ensure_workflow_dirs()
    if folder not in WORKFLOW_FOLDERS:
        raise ToolError(f"Unknown folder '{folder}'. Valid folders: {', '.join(WORKFLOW_FOLDERS)}")
    files = [p for p in (WORKFLOW_DIR / folder).iterdir() if p.is_file()]
    for f in files:
        f.unlink()
    return {"status": "cleared", "folder": folder, "deleted_count": len(files)}


@mcp.tool
def validate_loan_file(relative_path: str, ruleset_name: str) -> dict:
    """Validate a workflow loan file against a named ruleset and append a validation note.

    Use this — NOT the plain validate() tool — when the user asks to validate a loan
    that lives in the workflow (e.g. 'inbox/loan_0001.json'). Reads the file, runs
    validation, appends a 'passed-validated' or 'failed-validated' note, writes the
    file back, and returns both the validation results and the note text.

    Args:
        relative_path: Path relative to the workflow root, e.g. 'inbox/loan_0001.json'.
        ruleset_name: The name of the ruleset to validate against (use discover_rulesets()).

    Returns a dict with 'validation_results' (list of rule result dicts) and
    'note_appended' (the note text written into the file).
    """
    resolved_path, loan = _load_workflow_loan(relative_path)
    results = _get_service().validate("loan", loan, ruleset_name)
    failed = [r["rule_id"] for r in results if r.get("status") == "FAIL"]
    if failed:
        operation_type = "failed-validated"
        text = f"Validated against '{ruleset_name}': FAIL ({', '.join(failed)} failed)"
    else:
        operation_type = "passed-validated"
        text = f"Validated against '{ruleset_name}': all rules passed"
    writer = _make_writer(loan)
    writer.write(business_event=operation_type, message=text)
    resolved_path.write_text(json.dumps(writer.data, indent=2))
    return {"validation_results": results, "note_appended": text}


@mcp.tool
def batch_validate_loan_files(relative_paths: list, ruleset_name: str) -> dict:
    """Validate multiple workflow loan files against a named ruleset and append a note to each.

    Use this — NOT batch_validate() — when the user asks to validate several loans that
    live in the workflow at once. Each file is loaded, validated, and written back with a
    'passed-validated' or 'failed-validated' note. Files that cannot be read or parsed are
    recorded as errors without aborting the rest of the batch.

    Args:
        relative_paths: List of paths relative to the workflow root,
                        e.g. ['inbox/loan_0001.json', 'inbox/loan_0002.json'].
        ruleset_name: The name of the ruleset to validate against (use discover_rulesets()).

    Returns a dict with a 'results' list. Each entry is either a success dict with
    'relative_path', 'validation_results', and 'note_appended', or an error dict with
    'relative_path' and 'error'.
    """
    results = []
    for relative_path in relative_paths:
        try:
            resolved_path, loan = _load_workflow_loan(relative_path)
            validation_results = _get_service().validate("loan", loan, ruleset_name)
            failed = [r["rule_id"] for r in validation_results if r.get("status") == "FAIL"]
            if failed:
                operation_type = "failed-validated"
                text = f"Validated against '{ruleset_name}': FAIL ({', '.join(failed)} failed)"
            else:
                operation_type = "passed-validated"
                text = f"Validated against '{ruleset_name}': all rules passed"
            writer = _make_writer(loan)
            writer.write(business_event=operation_type, message=text)
            resolved_path.write_text(json.dumps(writer.data, indent=2))
            results.append({
                "relative_path": relative_path,
                "validation_results": validation_results,
                "note_appended": text,
            })
        except Exception as e:
            results.append({"relative_path": relative_path, "error": str(e)})
    return {"results": results}


@mcp.tool
def add_note(relative_path: str, text: str, operation_type: str = "note") -> dict:
    """Append a manual note entry to a workflow loan file.

    Args:
        relative_path: Path relative to the workflow root, e.g. 'inbox/loan_0001.json'.
        text: The note text (up to 1000 characters).
        operation_type: One of 'note', 'passed-validated', 'failed-validated', or 'edited'.
                        Defaults to 'note'.

    Returns the appended note entry dict.
    """
    valid_types = ["note", "passed-validated", "failed-validated", "edited"]
    if operation_type not in valid_types:
        raise ToolError(
            f"Invalid operation_type '{operation_type}'. Must be one of: {', '.join(valid_types)}"
        )
    resolved_path, loan = _load_workflow_loan(relative_path)
    writer = _make_writer(loan)
    writer.write(business_event=operation_type, message=text)
    resolved_path.write_text(json.dumps(writer.data, indent=2))
    return writer.data["notes"][-1]


@mcp.tool
def edit_loan_file(relative_path: str, changes: dict) -> dict:
    """Edit one or more fields of a workflow loan file and record an audit note.

    Accepts dot-notation field paths (e.g. 'financial.interest_rate') and applies
    all changes atomically. Captures old values before applying, writes an 'edited'
    note summarising every before/after change, then saves the file.

    Use this instead of write_workflow_file when making targeted field edits — it
    ensures every change is recorded in the audit log.

    Args:
        relative_path: Path relative to the workflow root, e.g. 'inbox/loan_0001.json'.
        changes: Dict mapping dot-notation field paths to new values,
                 e.g. {'financial.interest_rate': 0.07, 'status': 'closed'}.

    Returns a dict with 'status', 'changes' (list of {field, old, new} dicts),
    and 'note_appended' (the audit note text).
    """
    resolved_path, loan = _load_workflow_loan(relative_path)
    # Capture old values before mutation for the return dict
    change_records = [
        {"field": fp, "old": _get_nested(loan, fp), "new": nv}
        for fp, nv in changes.items()
    ]
    writer = _make_writer(loan)
    note_text = writer.write(business_event="edited", message="Edited", changes=changes)
    resolved_path.write_text(json.dumps(writer.data, indent=2))
    return {"status": "updated", "changes": change_records, "note_appended": note_text}


@mcp.resource(
    "ui://workflow/status",
    app=AppConfig(csp=ResourceCSP(resource_domains=["https://unpkg.com"])),
)
def workflow_status_ui() -> str:
    """MCP App UI resource — served as a sandboxed iframe in Claude Desktop."""
    return """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body  { margin:0; padding:8px; font-family:system-ui,sans-serif;
          background:#f8fafc; color:#334155; }
  .grid { display:grid; grid-template-columns:repeat(4,1fr); gap:6px; }
  .card { background:#fff; border:1px solid #e2e8f0; border-radius:6px;
          padding:8px 6px; text-align:center; }
  .n    { font-size:20px; font-weight:600; color:#3b82f6; }
  .lbl  { font-size:10px; color:#94a3b8; margin-top:2px; text-transform:uppercase; }
  .ts   { font-size:9px; color:#cbd5e1; text-align:right; margin-top:6px; }
</style></head><body>
<div class="grid">
  <div class="card"><div class="n" id="inbox">—</div><div class="lbl">Inbox</div></div>
  <div class="card"><div class="n" id="hold">—</div><div class="lbl">Hold</div></div>
  <div class="card"><div class="n" id="escalate">—</div><div class="lbl">Escalate</div></div>
  <div class="card"><div class="n" id="outbox">—</div><div class="lbl">Outbox</div></div>
</div>
<div class="ts" id="ts">loading\u2026</div>
<script type="module">
  import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";

  const app = new App({ name: "WorkflowStatus", version: "1.0.0" });

  function updateDisplay(content) {
    const text = content?.find(c => c.type === "text");
    if (!text) return;
    const { folders } = JSON.parse(text.text);
    ["inbox","hold","escalate","outbox"].forEach(f =>
      document.getElementById(f).textContent = folders[f]?.length ?? 0
    );
    document.getElementById("ts").textContent =
      "updated " + new Date().toLocaleTimeString();
  }

  await app.connect();

  // Request pip mode if the host supports it
  const context = app.getHostContext();
  if (context?.availableDisplayModes?.includes("pip")) {
    await app.requestDisplayMode({ mode: "pip" });
  }

  // Poll for updates every 10 seconds
  async function refresh() {
    const result = await app.callServerTool({ name: "get_workflow", arguments: {} });
    updateDisplay(result.content);
  }

  setInterval(refresh, 5000);
</script>
</body></html>"""


@mcp.tool(app=AppConfig(resource_uri="ui://workflow/status"))
def show_status() -> str:
    """Show a live workflow status dashboard. Renders an auto-refreshing panel in
    Claude Desktop showing counts for inbox, hold, escalate, and outbox.
    Trigger phrase: 'status'. The panel speaks for itself — do not add any text
    summary or commentary after calling this tool."""
    return "ok"


if __name__ == "__main__":
    mcp.run()
