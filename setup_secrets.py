"""
One-time setup script: creates the Databricks secret scope and stores the
Lakebase connection URL used by lakebase.py / the tickets app.

Run this locally (with the Databricks CLI configured) or from a notebook -
never commit the resulting secret value anywhere.

Usage:
    python setup_secrets.py
"""

import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

w = WorkspaceClient()

SCOPE = "database"
KEY = "lakebase-url"

try:
    w.secrets.create_scope(scope=SCOPE)
except Exception as exc:
    # Scope may already exist from a previous run.
    if "already exists" not in str(exc).lower():
        raise

w.secrets.put_secret(
    scope=SCOPE,
    key=KEY,
    string_value=getpass.getpass("Paste your Lakebase URL: "),
)

w.secrets.put_acl(
    scope=SCOPE,
    principal="users",
    permission=workspace.AclPermission.READ,
)

print(f"Stored secret {SCOPE}/{KEY}")
