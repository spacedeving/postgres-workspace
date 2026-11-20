# Postgres Workspace

A personal database app backed by PostgreSQL. Create typed tables in a browser, edit rows in a grid, inspect the same data with read-only SQL, and access it through table-scoped API keys.

## Start locally

Run `docker compose up --build`, open http://127.0.0.1:8000, and choose **Create account**. Use a unique password of at least 12 characters. PostgreSQL data persists in the Compose volume across restarts.

## Deploy a portfolio demo

The included `render.yaml` defines a Docker web service and a PostgreSQL database on Render. In a Render account, create a Blueprint from this repository, check the plan terms, and deploy it. Open the web service URL, create an account, and follow the walkthrough below. The Blueprint uses demo tiers. Review storage lifetime before relying on it for data you need to keep.

## Try it

1. Create an `inventory` table with `name:text` and `quantity:integer`.
2. Add a row and reload to check persistence.
3. In SQL editor, run `SELECT name, quantity FROM inventory WHERE quantity = 5;` against a row with quantity 5.
4. In API access, create a read key and send it as `Authorization: Bearer YOUR_KEY` to `GET /api/tables/inventory/rows`.
5. Create `applications` with `company:text` and `applied_on:date` to try a different schema.

## Implementation notes

Each account has a dedicated PostgreSQL schema. The server resolves it from an expiring session or a table-scoped API key and explicitly qualifies every user table. Passwords use salted scrypt hashes. Sessions and API keys are stored as hashes, and signing out invalidates the session. The SQL editor translates a restricted SELECT syntax into parameterized PostgreSQL statements. It supports column selection, equality filters, sorting, and up to 100 rows per query. Read/write keys can append rows and read keys can list rows. The grid supports row creation, editing, deletion, pagination, CSV import into an existing table, and export of the visible page. To link tables, create an integer column and use Add relationship to reference the other table's id.

GitHub Actions runs the smoke test against real PostgreSQL using Compose. The test exercises two accounts with identically named tables as well as SQL and API access. If you ran the prior single-password version, its data remains in the old workspace_data schema. New accounts get fresh workspaces, and the app does not automatically assign the old data to an account.
