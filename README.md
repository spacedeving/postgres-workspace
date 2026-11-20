# Postgres Workspace

A personal database app backed by PostgreSQL. Create typed tables in a browser, edit rows in a grid, inspect the same data with read-only SQL, and access it through table-scoped API keys.

## Start locally

Create a .env file containing WORKSPACE_PASSWORD with a strong password, then run `docker compose up --build`. Open http://127.0.0.1:8000 and enter that password. PostgreSQL data persists in the Compose volume across restarts.

## Try it

1. Create an `inventory` table with `name:text` and `quantity:integer`.
2. Add a row and reload to check persistence.
3. In SQL editor, run `SELECT name, quantity FROM inventory WHERE quantity = 5;` against a row with quantity 5.
4. In API access, create a read key and send it as `Authorization: Bearer YOUR_KEY` to `GET /api/tables/inventory/rows`.
5. Create `applications` with `company:text` and `applied_on:date` to try a different schema.

## Implementation notes

The Compose setup serves one password-protected workspace on loopback. Tables are real PostgreSQL tables in a dedicated schema. The SQL editor translates a restricted SELECT syntax into parameterized PostgreSQL statements. It supports column selection, equality filters, sorting, and up to 100 rows per query. The API stores only hashes of generated keys. Read/write keys can append rows and read keys can list rows. The grid supports row creation, editing, deletion, pagination, CSV import into an existing table, and export of the visible page. To link tables, create an integer column and use Add relationship to reference the other table's id.

GitHub Actions runs the smoke test against real PostgreSQL using Compose. The app currently targets one owner and one workspace. A hosted multi-user edition would need user accounts, workspace isolation, and a production deployment configuration.
