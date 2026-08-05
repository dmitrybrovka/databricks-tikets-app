"""
Databricks App:
- Serves a small Flask API + UI for support tickets
- Reads/writes to Lakebase (Databricks-managed Postgres) via lakebase.py

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import logging
import os
import uuid

from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, render_template, request

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tickets-app")

app = Flask(__name__)
_w = WorkspaceClient()

TICKETS_TABLE = os.environ.get("TICKETS_TABLE_NAME", "tickets")
MESSAGES_TABLE = os.environ.get("TICKET_MESSAGES_TABLE_NAME", "ticket_messages")

VALID_STATUSES = ("open", "in_progress", "solved")


def ensure_tables():
    """Create tickets and ticket_messages tables in Lakebase if they don't exist yet."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {TICKETS_TABLE} (
            ticket_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSAGES_TABLE} (
            message_id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL REFERENCES {TICKETS_TABLE}(ticket_id),
            message_text TEXT NOT NULL,
            author TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _current_user_email() -> str:
    """
    Resolve the current user's email.

    Databricks Apps inject the logged-in user's identity via the
    X-Forwarded-Email header on every request. Fall back to the Databricks
    SDK's current_user API for local development where that header isn't set.
    """
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    return _w.current_user.me().user_name


def _serialize_row(row: dict) -> dict:
    """Convert DB row values (e.g. datetime) into JSON-safe types."""
    out = dict(row)
    for key, value in out.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page)."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/")
def index():
    """Support tickets UI."""
    return render_template("index.html")


@app.route("/tickets", methods=["GET"])
def list_tickets():
    """Return all support tickets, newest first."""
    ensure_tables()
    rows = lakebase.run_query(
        f"""
        SELECT ticket_id, title, status, created_by, created_at
        FROM {TICKETS_TABLE}
        ORDER BY created_at DESC
        """
    )
    return jsonify([_serialize_row(r) for r in rows])


@app.route("/tickets", methods=["POST"])
def create_ticket():
    """Create a new support ticket."""
    ensure_tables()

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    status = (data.get("status") or "open").strip().lower()
    if status not in VALID_STATUSES:
        return jsonify(
            {"error": f"status must be one of: {', '.join(VALID_STATUSES)}"}
        ), 400

    ticket_id = str(uuid.uuid4())
    created_by = _current_user_email()

    lakebase.run_write(
        f"""
        INSERT INTO {TICKETS_TABLE} (ticket_id, title, status, created_by, created_at)
        VALUES (%s, %s, %s, %s, now())
        """,
        (ticket_id, title, status, created_by),
    )

    rows = lakebase.run_query(
        f"""
        SELECT ticket_id, title, status, created_by, created_at
        FROM {TICKETS_TABLE}
        WHERE ticket_id = %s
        """,
        (ticket_id,),
    )
    return jsonify(_serialize_row(rows[0])), 201


@app.route("/tickets/<ticket_id>", methods=["GET"])
def get_ticket(ticket_id: str):
    """Return a single ticket by id."""
    ensure_tables()
    rows = lakebase.run_query(
        f"""
        SELECT ticket_id, title, status, created_by, created_at
        FROM {TICKETS_TABLE}
        WHERE ticket_id = %s
        """,
        (ticket_id,),
    )
    if not rows:
        return jsonify({"error": "ticket not found"}), 404
    return jsonify(_serialize_row(rows[0]))


@app.route("/tickets/<ticket_id>/status", methods=["PATCH", "PUT"])
def update_ticket_status(ticket_id: str):
    """Update a ticket's status."""
    ensure_tables()

    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    if status not in VALID_STATUSES:
        return jsonify(
            {"error": f"status must be one of: {', '.join(VALID_STATUSES)}"}
        ), 400

    updated = lakebase.run_write(
        f"""
        UPDATE {TICKETS_TABLE}
        SET status = %s
        WHERE ticket_id = %s
        """,
        (status, ticket_id),
    )
    if updated == 0:
        return jsonify({"error": "ticket not found"}), 404

    rows = lakebase.run_query(
        f"""
        SELECT ticket_id, title, status, created_by, created_at
        FROM {TICKETS_TABLE}
        WHERE ticket_id = %s
        """,
        (ticket_id,),
    )
    return jsonify(_serialize_row(rows[0]))


@app.route("/tickets/<ticket_id>/messages", methods=["GET"])
def list_messages(ticket_id: str):
    """Return all messages for a ticket, oldest first."""
    ensure_tables()

    tickets = lakebase.run_query(
        f"SELECT ticket_id FROM {TICKETS_TABLE} WHERE ticket_id = %s",
        (ticket_id,),
    )
    if not tickets:
        return jsonify({"error": "ticket not found"}), 404

    rows = lakebase.run_query(
        f"""
        SELECT message_id, ticket_id, message_text, author, created_at
        FROM {MESSAGES_TABLE}
        WHERE ticket_id = %s
        ORDER BY created_at ASC
        """,
        (ticket_id,),
    )
    return jsonify([_serialize_row(r) for r in rows])


@app.route("/tickets/<ticket_id>/messages", methods=["POST"])
def add_message(ticket_id: str):
    """Add a message to an existing ticket."""
    ensure_tables()

    tickets = lakebase.run_query(
        f"SELECT ticket_id FROM {TICKETS_TABLE} WHERE ticket_id = %s",
        (ticket_id,),
    )
    if not tickets:
        return jsonify({"error": "ticket not found"}), 404

    data = request.get_json(silent=True) or {}
    message_text = (data.get("message_text") or "").strip()
    if not message_text:
        return jsonify({"error": "message_text is required"}), 400

    message_id = str(uuid.uuid4())
    author = _current_user_email()

    lakebase.run_write(
        f"""
        INSERT INTO {MESSAGES_TABLE}
            (message_id, ticket_id, message_text, author, created_at)
        VALUES (%s, %s, %s, %s, now())
        """,
        (message_id, ticket_id, message_text, author),
    )

    rows = lakebase.run_query(
        f"""
        SELECT message_id, ticket_id, message_text, author, created_at
        FROM {MESSAGES_TABLE}
        WHERE message_id = %s
        """,
        (message_id,),
    )
    return jsonify(_serialize_row(rows[0])), 201


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", 8000))
    app.run(debug=True, host=host, port=port)
    print(f"Flask app running on http://{host}:{port}")
