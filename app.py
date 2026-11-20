import csv
import hashlib
import io
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

@asynccontextmanager
async def lifespan(app):
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        initialize(db)
    yield


app = FastAPI(title="Postgres Workspace", lifespan=lifespan)
TYPES = {"text": "text", "integer": "bigint", "decimal": "numeric",
         "boolean": "boolean", "date": "date"}
NAME = r"[a-z][a-z0-9_]{0,47}"


def identifier(name):
    if not re.fullmatch(NAME, name):
        raise HTTPException(422, "Use lowercase letters, digits and underscores.")
    return sql.Identifier(name)


def database():
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as connection:
        connection.execute("SET LOCAL statement_timeout = '5s'")
        yield connection


DB = Depends(database, scope="function")


def initialize(db):
    db.execute("SELECT pg_advisory_xact_lock(63491721)")
    db.execute("""
        CREATE SCHEMA IF NOT EXISTS workspace_auth;
        CREATE TABLE IF NOT EXISTS workspace_auth.accounts (
            username text PRIMARY KEY, salt bytea NOT NULL,
            verifier bytea NOT NULL, workspace text UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_auth.sessions (
            digest text PRIMARY KEY, workspace text NOT NULL
            REFERENCES workspace_auth.accounts(workspace),
            expires_at timestamptz NOT NULL DEFAULT (now() + interval '12 hours')
        );
        CREATE TABLE IF NOT EXISTS workspace_auth.keys (
            digest text PRIMARY KEY, workspace text NOT NULL
            REFERENCES workspace_auth.accounts(workspace),
            table_name text NOT NULL, writable boolean NOT NULL
        );
    """)


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)


def session(db, workspace):
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO workspace_auth.sessions (digest, workspace) VALUES (%s, %s)",
               (digest(token), workspace))
    return {"token": token}


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=48)
    password: str = Field(min_length=12, max_length=128)


@app.post("/accounts", status_code=201)
def register(credentials: Credentials, db=DB):
    username = credentials.username.strip().lower()
    identifier(username)
    salt = secrets.token_bytes(16)
    workspace = "ws_" + uuid4().hex
    account = db.execute(
        "INSERT INTO workspace_auth.accounts VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (username) DO NOTHING RETURNING workspace",
        (username, salt, password_hash(credentials.password, salt), workspace),
    ).fetchone()
    if account is None:
        raise HTTPException(409, "Username is already taken.")
    db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(workspace)))
    return session(db, workspace)


@app.post("/sessions")
def login(credentials: Credentials, db=DB):
    account = db.execute("SELECT * FROM workspace_auth.accounts WHERE username = %s",
                         (credentials.username.strip().lower(),)).fetchone()
    verifier = password_hash(credentials.password, account["salt"] if account else bytes(16))
    if account is None or not secrets.compare_digest(verifier, account["verifier"]):
        raise HTTPException(401, "Username or password is incorrect.")
    return session(db, account["workspace"])


def bearer(authorization):
    if not authorization.startswith("Bearer ") or len(authorization) > 100:
        raise HTTPException(401, "Sign in or provide a valid API key.")
    return digest(authorization[7:])


def select_workspace(db, workspace):
    db.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(workspace)))


def owner(authorization: str = Header(default=""), db=DB):
    record = db.execute("SELECT workspace FROM workspace_auth.sessions "
                        "WHERE digest = %s AND expires_at > now()",
                        (bearer(authorization),)).fetchone()
    if record is None:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    select_workspace(db, record["workspace"])
    return db


OWNER = Depends(owner, scope="function")


@app.delete("/sessions/current")
def logout(authorization: str = Header(default=""), db=DB):
    db.execute("DELETE FROM workspace_auth.sessions WHERE digest = %s", (bearer(authorization),))
    return {"signed_out": True}


@app.exception_handler(psycopg.Error)
async def postgres_error(request, error):
    return JSONResponse({"detail": error.diag.message_primary or "Database request failed"}, status_code=400)


@app.get("/")
def home():
    return FileResponse(Path(__file__).with_name("index.html"))


def table(db, name):
    identifier(name)
    workspace = db.execute("SELECT current_schema() AS name").fetchone()["name"]
    return sql.Identifier(workspace, name)


@app.get("/tables")
def tables(db=OWNER):
    return db.execute(
        "SELECT table_name, json_object_agg(column_name, data_type ORDER BY ordinal_position) AS columns "
        "FROM information_schema.columns WHERE table_schema = current_schema() "
        "GROUP BY table_name ORDER BY table_name"
    ).fetchall()


@app.post("/tables", status_code=201)
def create(body: dict, db=OWNER):
    name, columns = body.get("name", ""), body.get("columns")
    if not isinstance(name, str) or not isinstance(columns, dict) or not 1 <= len(columns) <= 40:
        raise HTTPException(422, "Provide a table name and 1 to 40 columns.")
    definitions = [sql.SQL("id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY")]
    for column, kind in columns.items():
        if not isinstance(kind, str) or kind not in TYPES or column == "id":
            raise HTTPException(422, "Choose a supported type and reserve id for the primary key.")
        definitions.append(sql.SQL("{} {}").format(identifier(column), sql.SQL(TYPES[kind])))
    db.execute(sql.SQL("CREATE TABLE {} ({})").format(table(db, name), sql.SQL(", ").join(definitions)))
    return {"name": name}


def rows(db, name, offset=0):
    return db.execute(sql.SQL("SELECT * FROM {} ORDER BY id LIMIT 100 OFFSET %s").format(table(db, name)),
                      (offset,)).fetchall()


def save(db, name, data, row_id=None):
    if not data or len(data) > 40 or "id" in data or any(
        value is not None and type(value) not in (str, int, float, bool) for value in data.values()
    ):
        raise HTTPException(422, "Supply one or more scalar column values.")
    columns = [identifier(key) for key in data]
    if row_id is None:
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            table(db, name), sql.SQL(", ").join(columns), sql.SQL(", ").join([sql.Placeholder()] * len(columns)))
        params = list(data.values())
    else:
        query = sql.SQL("UPDATE {} SET {} WHERE id = %s RETURNING *").format(
            table(db, name), sql.SQL(", ").join(sql.SQL("{} = %s").format(column) for column in columns))
        params = [*data.values(), row_id]
    result = db.execute(query, params).fetchone()
    if result is None:
        raise HTTPException(404, "Row not found.")
    return result


@app.get("/tables/{name}/rows")
def get_rows(name: str, offset: int = 0, db=OWNER):
    return rows(db, name, max(0, offset))


@app.post("/tables/{name}/import")
def import_csv(name: str, body: dict, db=OWNER):
    content = body.get("csv")
    if not isinstance(content, str) or len(content) > 2_000_000:
        raise HTTPException(422, "Choose a CSV file under 2 MB.")
    reader = csv.DictReader(io.StringIO(content.removeprefix("\ufeff")), strict=True)
    count = 0
    try:
        headers = [header for header in (reader.fieldnames or []) if header != "id"]
        if not headers or len(headers) != len(set(headers)):
            raise HTTPException(422, "Use unique column names in the CSV header.")
        for row in reader:
            if count == 1000 or None in row or any(value is None for value in row.values()):
                raise HTTPException(422, "Use at most 1,000 rows, each matching the CSV header.")
            save(db, name, {header: row[header] for header in headers})
            count += 1
    except csv.Error as error:
        raise HTTPException(422, str(error)) from error
    return {"imported": count}


@app.post("/tables/{name}/relationships")
def relationship(name: str, body: dict, db=OWNER):
    column, target = body.get("column"), body.get("target")
    if not isinstance(column, str) or not isinstance(target, str) or column == "id":
        raise HTTPException(422, "Select a column and a referenced table.")
    db.execute(sql.SQL("ALTER TABLE {} ADD FOREIGN KEY ({}) REFERENCES {} (id)").format(
        table(db, name), identifier(column), table(db, target)))
    return {"column": column, "target": target}


@app.post("/tables/{name}/rows")
def insert(name: str, body: dict, db=OWNER):
    return save(db, name, body)


@app.patch("/tables/{name}/rows/{row_id}")
def update(name: str, row_id: int, body: dict, db=OWNER):
    return save(db, name, body, row_id)


@app.delete("/tables/{name}/rows/{row_id}")
def delete(name: str, row_id: int, db=OWNER):
    if not db.execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(table(db, name)), (row_id,)).rowcount:
        raise HTTPException(404, "Row not found.")
    return {"deleted": row_id}


@app.post("/query")
def query(body: dict, db=OWNER):
    statement = body.get("sql", "")
    match = re.fullmatch(
        rf"SELECT\s+(\*|{NAME}(?:\s*,\s*{NAME})*)\s+FROM\s+({NAME})"
        rf"(?:\s+WHERE\s+({NAME})\s*=\s*('(?:[^']|'')*'|-?\d+|true|false))?"
        rf"(?:\s+ORDER\s+BY\s+({NAME})(?:\s+(ASC|DESC))?)?\s*;?",
        statement.strip() if isinstance(statement, str) else "", re.IGNORECASE,
    )
    if not match:
        raise HTTPException(422, "Use SELECT columns FROM table with optional WHERE column = value and ORDER BY.")
    projection, name, where, value, order, direction = match.groups()
    columns = sql.SQL("*") if projection == "*" else sql.SQL(", ").join(
        identifier(column.strip().lower()) for column in projection.split(","))
    command = sql.SQL("SELECT {} FROM {}").format(columns, table(db, name.lower()))
    params = ()
    if where:
        command += sql.SQL(" WHERE {} = %s").format(identifier(where.lower()))
        params = (value[1:-1].replace("''", "'") if value.startswith("'") else value.lower(),)
    if order:
        command += sql.SQL(" ORDER BY {} {}").format(identifier(order.lower()),
                                                     sql.SQL((direction or "ASC").upper()))
    cursor = db.execute(command + sql.SQL(" LIMIT 100"), params)
    return {"columns": [column.name for column in cursor.description], "rows": cursor.fetchall()}


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@app.post("/tables/{name}/keys")
def issue_key(name: str, body: dict, db=OWNER):
    db.execute(sql.SQL("SELECT 1 FROM {} LIMIT 0").format(table(db, name)))
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO workspace_auth.keys VALUES (%s, current_schema(), %s, %s)",
               (digest(token), name, body.get("writable") is True))
    return {"key": token}


@app.delete("/tables/{name}/keys")
def revoke_keys(name: str, db=OWNER):
    return {"revoked": db.execute("DELETE FROM workspace_auth.keys WHERE workspace = current_schema() AND table_name = %s", (name,)).rowcount}


def authorize(db, token, name, write=False):
    key = db.execute("SELECT workspace, writable FROM workspace_auth.keys "
                     "WHERE digest = %s AND table_name = %s",
                     (bearer(token), name)).fetchone()
    if key is None or (write and not key["writable"]):
        raise HTTPException(403, "This API key lacks permission.")
    select_workspace(db, key["workspace"])


@app.get("/api/tables/{name}/rows")
def api_get(name: str, offset: int = 0, authorization: str = Header(default=""), db=DB):
    authorize(db, authorization, name)
    return rows(db, name, max(0, offset))


@app.post("/api/tables/{name}/rows")
def api_post(name: str, body: dict, authorization: str = Header(default=""), db=DB):
    authorize(db, authorization, name, write=True)
    return save(db, name, body)
