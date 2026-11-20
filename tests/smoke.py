import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(path, method="GET", body=None, token="test-password", expected=200):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    try:
        response = urlopen(Request("http://127.0.0.1:8000" + path, data, headers, method=method), timeout=10)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read()
        assert response.status == expected, (path, response.status, payload)
        return json.loads(payload) if response.headers.get_content_type() == "application/json" else payload


assert b"Postgres Workspace" in request("/")
request("/tables", token="wrong", expected=401)
for name, columns in [
    ("inventory", {"name": "text", "quantity": "integer"}),
    ("applications", {"company": "text", "applied_on": "date", "item_id": "integer"}),
]:
    request("/tables", "POST", {"name": name, "columns": columns}, expected=201)

inventory = "/tables/inventory"
applications = "/tables/applications"
row = request(inventory + "/rows", "POST", {"name": "Socket", "quantity": "5"})
assert row["quantity"] == 5
assert request(inventory + "/rows")[0] == row
assert request("/query", "POST", {"sql": "SELECT name FROM inventory WHERE quantity = 5"})["rows"] == [
    {"name": "Socket"}
]
request("/query", "POST", {"sql": "DELETE FROM inventory"}, expected=422)
request(applications + "/relationships", "POST", {"column": "item_id", "target": "inventory"})
request(applications + "/import", "POST", {"csv": "company,applied_on,item_id\nAcme,2026-11-20,1\n"})
assert request(applications + "/rows")[0]["company"] == "Acme"

key = request(inventory + "/keys", "POST", {})["key"]
assert request("/api" + inventory + "/rows", token=key)[0] == row
request("/api" + applications + "/rows", token=key, expected=403)
request("/api" + inventory + "/rows", "POST", {"name": "Blocked"}, token=key, expected=403)
write_key = request(inventory + "/keys", "POST", {"writable": True})["key"]
created = request("/api" + inventory + "/rows", "POST", {"name": "Wrench"}, token=write_key)
assert request(inventory + "/rows/" + str(created["id"]), "PATCH", {"quantity": 12})["quantity"] == 12
request(inventory + "/rows/" + str(created["id"]), "DELETE")
request(inventory + "/keys", "DELETE")
request("/api" + inventory + "/rows", token=key, expected=403)
print("PostgreSQL workspace smoke test passed")
