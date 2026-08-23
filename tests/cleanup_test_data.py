"""One-off cleanup of QA-created bookings/gemeinden (prefix TEST_ or specific notes)."""
import os
import requests
from dotenv import dotenv_values

BASE = (os.environ.get("REACT_APP_BACKEND_URL") or dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"]).rstrip("/")
API = f"{BASE}/api"

tok = requests.post(f"{API}/admin/login", json={"password": "herisau2026"}).json()["token"]
h = {"X-Admin-Password": tok}

bookings = requests.get(f"{API}/bookings", headers=h).json()["bookings"]
for b in bookings:
    if b["municipality"].startswith("TEST_") or "TEST " in (b.get("note") or "") or (b.get("note") or "").startswith("TEST"):
        r = requests.delete(f"{API}/bookings/{b['id']}", headers=h)
        print("deleted booking", b["date"], b["slot_index"], b["municipality"], r.status_code)

for g in requests.get(f"{API}/gemeinden", headers=h).json()["gemeinden"]:
    if g["name"].startswith("TEST_"):
        print("deleted gemeinde", g["name"], requests.delete(f"{API}/gemeinden/{g['id']}", headers=h).status_code)

print("remaining bookings:", len(requests.get(f"{API}/bookings", headers=h).json()["bookings"]))
print("remaining gemeinden:", [g["username"] for g in requests.get(f"{API}/gemeinden", headers=h).json()["gemeinden"]])
