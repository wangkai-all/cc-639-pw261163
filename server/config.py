"""Central configuration for the workstation and the demo imaging service."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RECEIVED_DIR = DATA_DIR / "received"      # accepted instances (may hold conflict versions)
QUARANTINE_DIR = DATA_DIR / "quarantine"  # file-meta / dataset identifier mismatches
EXPORT_DIR = DATA_DIR / "exports"         # generated download packages
INDEX_PATH = DATA_DIR / "index.json"      # local store index

# --- Demo imaging service (the remote "PACS" we query and retrieve from) ---
DEMO_AE_TITLE = "DEMOQRSCP"
DEMO_HOST = "127.0.0.1"
DEMO_PORT = 11112

# --- This workstation ---
WS_AE_TITLE = "WEBWS"           # our AE title (move destination / calling AE)
WS_CSTORE_HOST = "127.0.0.1"
WS_CSTORE_PORT = 11113          # our C-STORE SCP that receives C-MOVE sub-operations

API_HOST = "127.0.0.1"
API_PORT = 8000

# AE title -> (host, port) resolution table used by the demo service to
# deliver C-MOVE sub-operations.
MOVE_DESTINATIONS = {
    WS_AE_TITLE: (WS_CSTORE_HOST, WS_CSTORE_PORT),
}

for _p in (DATA_DIR, RECEIVED_DIR, QUARANTINE_DIR, EXPORT_DIR):
    _p.mkdir(parents=True, exist_ok=True)
