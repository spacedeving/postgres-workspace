import csv
import hashlib
import io
import os
import re
import secrets
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from psycopg import sql
from psycopg.rows import dict_row

app = FastAPI(title="Postgres Workspace")
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


def owner(authorization: str = Header(default="")):
    password = os.environ["WORKSPACE_PASSWORD"]
    if not password or not secrets.compare_digest(authorization.encode(), ("Bearer " + password).encode()):
        raise HTTPException(401, "Enter the workspace password.")


DB = Depends(database, scope="function")


@app.exception_handler(psycopg.Error)
async def postgres_error(request, error):
    return JSONResponse({"detail": error.diag.message_primary or "Database request failed"}, status_code=400)


@app.get("/")
def home():
    return FileResponse(Path(__file__).with_name("index.html"))


def table(name):
    identifier(name)
    return sql.Identifier("workspace_data", name)


def metadata(db):
    db.execute("SELECT pg_advisory_xact_lock(63491720)")
    db.execute("CREATE SCHEMA IF NOT EXISTS workspace_data")
    db.execute("CREATE TABLE IF NOT EXISTS public.workspace_keys "
               "(digest text PRIMARY KEY, table_name text NOT NULL, writable boolean NOT NULL)")


@app.get("/tables", dependencies=[Depends(owner)])
def tables(db=DB):
    return db.execute(
        "SELECT table_name, json_object_agg(column_name, data_type ORDER BY ordinal_position) AS columns "
        "FROM information_schema.columns WHERE table_schema = 'workspace_data' "
        "GROUP BY table_name ORDER BY table_name"
    ).fetchall()


@app.post("/tables", dependencies=[Depends(owner)], status_code=201)
def create(body: dict, db=DB):
    name, columns = body.get("name", ""), body.get("columns")
    if not isinstance(name, str) or not isinstance(columns, dict) or not 1 <= len(columns) <= 40:
        raise HTTPException(422, "Provide a table name and 1 to 40 columns.")
    definitions = [sql.SQL("id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY")]
    for column, kind in columns.items():
        if not isinstance(kind, str) or kind not in TYPES or column == "id":
            raise HTTPException(422, "Choose a supported type and reserve id for the primary key.")
        definitions.append(sql.SQL("{} {}").format(identifier(column), sql.SQL(TYPES[kind])))
    metadata(db)
    db.execute(sql.SQL("CREATE TABLE {} ({})").format(table(name), sql.SQL(", ").join(definitions)))
    return {"name": name}


def rows(db, name, offset=0):
    return db.execute(sql.SQL("SELECT * FROM {} ORDER BY id LIMIT 100 OFFSET %s").format(table(name)),
                      (offset,)).fetchall()


def save(db, name, data, row_id=None):
    if not data or len(data) > 40 or "id" in data or any(
        value is not None and type(value) not in (str, int, float, bool) for value in data.values()
    ):
        raise HTTPException(422, "Supply one or more scalar column values.")
    columns = [identifier(key) for key in data]
    if row_id is None:
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            table(name), sql.SQL(", ").join(columns), sql.SQL(", ").join([sql.Placeholder()] * len(columns)))
        params = list(data.values())
    else:
        query = sql.SQL("UPDATE {} SET {} WHERE id = %s RETURNING *").format(
            table(name), sql.SQL(", ").join(sql.SQL("{} = %s").format(column) for column in columns))
        params = [*data.values(), row_id]
    result = db.execute(query, params).fetchone()
    if result is None:
        raise HTTPException(404, "Row not found.")
    return result


@app.get("/tables/{name}/rows", dependencies=[Depends(owner)])
def get_rows(name: str, offset: int = 0, db=DB):
    return rows(db, name, max(0, offset))


@app.post("/tables/{name}/import", dependencies=[Depends(owner)])
def import_csv(name: str, body: dict, db=DB):
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


@app.post("/tables/{name}/relationships", dependencies=[Depends(owner)])
def relationship(name: str, body: dict, db=DB):
    column, target = body.get("column"), body.get("target")
    if not isinstance(column, str) or not isinstance(target, str) or column == "id":
        raise HTTPException(422, "Select a column and a referenced table.")
    db.execute(sql.SQL("ALTER TABLE {} ADD FOREIGN KEY ({}) REFERENCES {} (id)").format(
        table(name), identifier(column), table(target)))
    return {"column": column, "target": target}


@app.post("/tables/{name}/rows", dependencies=[Depends(owner)])
def insert(name: str, body: dict, db=DB):
    return save(db, name, body)


@app.patch("/tables/{name}/rows/{row_id}", dependencies=[Depends(owner)])
def update(name: str, row_id: int, body: dict, db=DB):
    return save(db, name, body, row_id)


@app.delete("/tables/{name}/rows/{row_id}", dependencies=[Depends(owner)])
def delete(name: str, row_id: int, db=DB):
    if not db.execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(table(name)), (row_id,)).rowcount:
        raise HTTPException(404, "Row not found.")
    return {"deleted": row_id}


@app.post("/query", dependencies=[Depends(owner)])
def query(body: dict, db=DB):
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
    command = sql.SQL("SELECT {} FROM {}").format(columns, table(name.lower()))
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


@app.post("/tables/{name}/keys", dependencies=[Depends(owner)])
def issue_key(name: str, body: dict, db=DB):
    db.execute(sql.SQL("SELECT 1 FROM {} LIMIT 0").format(table(name)))
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO public.workspace_keys VALUES (%s, %s, %s)",
               (digest(token), name, body.get("writable") is True))
    return {"key": token}


@app.delete("/tables/{name}/keys", dependencies=[Depends(owner)])
def revoke_keys(name: str, db=DB):
    return {"revoked": db.execute("DELETE FROM public.workspace_keys WHERE table_name = %s", (name,)).rowcount}


def authorize(db, token, name, write=False):
    if not token.startswith("Bearer "):
        raise HTTPException(401, "Provide a table API key.")
    key = db.execute("SELECT writable FROM public.workspace_keys WHERE digest = %s AND table_name = %s",
                     (digest(token[7:]), name)).fetchone()
    if key is None or (write and not key["writable"]):
        raise HTTPException(403, "This API key lacks permission.")


@app.get("/api/tables/{name}/rows")
def api_get(name: str, offset: int = 0, authorization: str = Header(default=""), db=DB):
    authorize(db, authorization, name)
    return rows(db, name, max(0, offset))


@app.post("/api/tables/{name}/rows")
def api_post(name: str, body: dict, authorization: str = Header(default=""), db=DB):
    authorize(db, authorization, name, write=True)
    return save(db, name, body)
