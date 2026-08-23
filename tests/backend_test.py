"""Backend tests for Schularzt Herisau App - iteration 2 features."""
import os
import requests
import pytest
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_PW = "herisau2026"
GEM_USER = "waldstatt"
GEM_PW = "waldstatt2026"


@pytest.fixture(scope="session")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token(client):
    r = client.post(f"{API}/admin/login", json={"password": ADMIN_PW})
    if r.status_code != 200:
        pytest.fail(f"Admin login failed: {r.status_code} {r.text[:300]}")
    return r.json()["token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token):
    return {"X-Admin-Password": admin_token}


@pytest.fixture(scope="session")
def gemeinde_token(client):
    r = client.post(f"{API}/gemeinde/login", json={"username": GEM_USER, "password": GEM_PW})
    if r.status_code != 200:
        pytest.fail(f"Gemeinde login failed: {r.status_code} {r.text[:300]}")
    return r.json()["token"]


@pytest.fixture(scope="session")
def gemeinde_headers(gemeinde_token):
    return {"Authorization": f"Bearer {gemeinde_token}"}


# ---------------- Health / config ----------------
class TestHealth:
    def test_root(self, client):
        r = client.get(f"{API}/")
        assert r.status_code == 200
        assert "message" in r.json()

    def test_config(self, client):
        r = client.get(f"{API}/config")
        assert r.status_code == 200
        d = r.json()
        assert len(d["time_slots"]) == 16
        assert d["school_year_start"] == "2026-08-17"

    def test_days(self, client):
        r = client.get(f"{API}/days")
        assert r.status_code == 200
        days = r.json()["days"]
        assert len(days) > 300
        assert days[0]["date"] == "2026-08-17"


# ---------------- Admin login ----------------
class TestAdminLogin:
    def test_login_success(self, client):
        r = client.post(f"{API}/admin/login", json={"password": ADMIN_PW})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert isinstance(d["token"], str) and len(d["token"]) > 20

    def test_login_wrong_password(self, client):
        r = client.post(f"{API}/admin/login", json={"password": "wrong-pw-xyz"})
        assert r.status_code == 401

    def test_protected_requires_token(self, client):
        r = client.get(f"{API}/bookings")
        assert r.status_code == 401

    def test_protected_rejects_raw_password(self, client):
        # legacy header must contain JWT, not the plain password
        r = client.get(f"{API}/bookings", headers={"X-Admin-Password": ADMIN_PW})
        assert r.status_code == 401

    def test_protected_with_token(self, client, admin_headers):
        r = client.get(f"{API}/bookings", headers=admin_headers)
        assert r.status_code == 200
        assert isinstance(r.json()["bookings"], list)


# ---------------- Admin change password ----------------
class TestAdminChangePassword:
    def test_change_and_revert(self, client, admin_headers):
        new_pw = "TEST_neuespw1"
        try:
            # too short
            r = client.post(f"{API}/admin/change-password", headers=admin_headers,
                            json={"current_password": ADMIN_PW, "new_password": "abc"})
            assert r.status_code == 400, r.text

            # same as current
            r = client.post(f"{API}/admin/change-password", headers=admin_headers,
                            json={"current_password": ADMIN_PW, "new_password": ADMIN_PW})
            assert r.status_code == 400, r.text

            # wrong current
            r = client.post(f"{API}/admin/change-password", headers=admin_headers,
                            json={"current_password": "nope123456", "new_password": new_pw})
            assert r.status_code == 401, r.text

            # no auth
            r = client.post(f"{API}/admin/change-password",
                            json={"current_password": ADMIN_PW, "new_password": new_pw})
            assert r.status_code == 401

            # success
            r = client.post(f"{API}/admin/change-password", headers=admin_headers,
                            json={"current_password": ADMIN_PW, "new_password": new_pw})
            assert r.status_code == 200, r.text
            new_token = r.json()["token"]
            assert isinstance(new_token, str) and len(new_token) > 20

            # old password now fails
            assert client.post(f"{API}/admin/login", json={"password": ADMIN_PW}).status_code == 401
            # new password works
            r2 = client.post(f"{API}/admin/login", json={"password": new_pw})
            assert r2.status_code == 200
            tok2 = r2.json()["token"]

            # revert
            rr = client.post(f"{API}/admin/change-password",
                             headers={"X-Admin-Password": tok2},
                             json={"current_password": new_pw, "new_password": ADMIN_PW})
            assert rr.status_code == 200, rr.text
        finally:
            # ensure reverted no matter what
            if client.post(f"{API}/admin/login", json={"password": ADMIN_PW}).status_code != 200:
                r = client.post(f"{API}/admin/login", json={"password": new_pw})
                if r.status_code == 200:
                    client.post(f"{API}/admin/change-password",
                                headers={"X-Admin-Password": r.json()["token"]},
                                json={"current_password": new_pw, "new_password": ADMIN_PW})
        assert client.post(f"{API}/admin/login", json={"password": ADMIN_PW}).status_code == 200


# ---------------- Gemeinden CRUD ----------------
class TestGemeindenCRUD:
    created = []

    def test_list_requires_admin(self, client):
        assert client.get(f"{API}/gemeinden").status_code == 401

    def test_list_gemeinden_no_password_hash(self, client, admin_headers):
        r = client.get(f"{API}/gemeinden", headers=admin_headers)
        assert r.status_code == 200
        gs = r.json()["gemeinden"]
        assert any(g["username"] == GEM_USER for g in gs)
        for g in gs:
            assert "password_hash" not in g
            assert "_id" not in g

    def test_create_update_delete(self, client, admin_headers):
        payload = {"name": "TEST_Gemeinde A", "username": "TEST_gemA", "password": "abcd1234"}
        r = client.post(f"{API}/gemeinden", headers=admin_headers, json=payload)
        assert r.status_code == 200, r.text
        g = r.json()
        gid = g["id"]
        self.created.append(gid)
        assert g["name"] == "TEST_Gemeinde A"
        assert g["username"] == "test_gema"  # lowercased
        assert "password_hash" not in g

        # verify persisted
        lst = client.get(f"{API}/gemeinden", headers=admin_headers).json()["gemeinden"]
        assert any(x["id"] == gid for x in lst)

        # login with created gemeinde
        lr = client.post(f"{API}/gemeinde/login", json={"username": "test_gema", "password": "abcd1234"})
        assert lr.status_code == 200, lr.text
        assert lr.json()["gemeinde"]["name"] == "TEST_Gemeinde A"

        # duplicate username -> 409
        dup = client.post(f"{API}/gemeinden", headers=admin_headers, json=payload)
        assert dup.status_code == 409, dup.text

        # short password -> 400
        short = client.post(f"{API}/gemeinden", headers=admin_headers,
                            json={"name": "TEST_B", "username": "TEST_gemB", "password": "abc"})
        assert short.status_code == 400, short.text

        # update name + password
        ur = client.put(f"{API}/gemeinden/{gid}", headers=admin_headers,
                        json={"name": "TEST_Gemeinde A2", "password": "wxyz9876"})
        assert ur.status_code == 200, ur.text
        lst = client.get(f"{API}/gemeinden", headers=admin_headers).json()["gemeinden"]
        upd = next(x for x in lst if x["id"] == gid)
        assert upd["name"] == "TEST_Gemeinde A2"
        assert client.post(f"{API}/gemeinde/login", json={"username": "test_gema", "password": "wxyz9876"}).status_code == 200
        assert client.post(f"{API}/gemeinde/login", json={"username": "test_gema", "password": "abcd1234"}).status_code == 401

        # update short password -> 400
        assert client.put(f"{API}/gemeinden/{gid}", headers=admin_headers, json={"password": "ab"}).status_code == 400
        # no changes -> 400
        assert client.put(f"{API}/gemeinden/{gid}", headers=admin_headers, json={}).status_code == 400
        # unknown id -> 404
        assert client.put(f"{API}/gemeinden/does-not-exist", headers=admin_headers, json={"name": "X"}).status_code == 404

        # delete
        dr = client.delete(f"{API}/gemeinden/{gid}", headers=admin_headers)
        assert dr.status_code == 200
        self.created.remove(gid)
        lst = client.get(f"{API}/gemeinden", headers=admin_headers).json()["gemeinden"]
        assert not any(x["id"] == gid for x in lst)
        assert client.delete(f"{API}/gemeinden/{gid}", headers=admin_headers).status_code == 404

    @pytest.fixture(scope="class", autouse=True)
    def cleanup(self, client):
        yield
        r = client.post(f"{API}/admin/login", json={"password": ADMIN_PW})
        if r.status_code == 200:
            h = {"X-Admin-Password": r.json()["token"]}
            for g in client.get(f"{API}/gemeinden", headers=h).json().get("gemeinden", []):
                if g["name"].startswith("TEST_"):
                    client.delete(f"{API}/gemeinden/{g['id']}", headers=h)


# ---------------- Gemeinde login & data ----------------
class TestGemeindeAuth:
    def test_login_success(self, client):
        r = client.post(f"{API}/gemeinde/login", json={"username": GEM_USER, "password": GEM_PW})
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["token"], str)
        assert d["gemeinde"]["name"] == "Schule Waldstatt"
        assert d["gemeinde"]["username"] == GEM_USER
        assert "password_hash" not in d["gemeinde"]

    def test_login_wrong_password(self, client):
        r = client.post(f"{API}/gemeinde/login", json={"username": GEM_USER, "password": "bad"})
        assert r.status_code == 401

    def test_login_unknown_user(self, client):
        r = client.post(f"{API}/gemeinde/login", json={"username": "nobody123", "password": "bad"})
        assert r.status_code == 401

    def test_me(self, client, gemeinde_headers):
        r = client.get(f"{API}/gemeinde/me", headers=gemeinde_headers)
        assert r.status_code == 200
        assert r.json()["username"] == GEM_USER

    def test_me_requires_token(self, client):
        assert client.get(f"{API}/gemeinde/me").status_code == 401
        assert client.get(f"{API}/gemeinde/me", headers={"Authorization": "Bearer garbage"}).status_code == 401

    def test_bookings_requires_token(self, client):
        assert client.get(f"{API}/gemeinde/bookings").status_code == 401

    def test_bookings_scoped(self, client, gemeinde_headers):
        r = client.get(f"{API}/gemeinde/bookings", headers=gemeinde_headers)
        assert r.status_code == 200
        me = client.get(f"{API}/gemeinde/me", headers=gemeinde_headers).json()
        for b in r.json()["bookings"]:
            assert b["gemeinde_id"] == me["id"]
            assert "_id" not in b

    def test_pdf(self, client, gemeinde_headers):
        r = client.get(f"{API}/gemeinde/bookings/pdf", headers=gemeinde_headers)
        assert r.status_code == 200, r.text[:300]
        assert r.headers.get("content-type", "").startswith("application/pdf")
        assert "attachment" in r.headers.get("content-disposition", "")
        assert r.content[:4] == b"%PDF"

    def test_pdf_requires_token(self, client):
        assert client.get(f"{API}/gemeinde/bookings/pdf").status_code == 401


# ---------------- Bookings (single + batch) ----------------
TEST_DATE = "2026-11-10"  # Tuesday, free


class TestBookings:
    ids = []

    @pytest.fixture(scope="class", autouse=True)
    def cleanup(self, client):
        yield
        r = client.post(f"{API}/admin/login", json={"password": ADMIN_PW})
        if r.status_code == 200:
            h = {"X-Admin-Password": r.json()["token"]}
            for b in client.get(f"{API}/bookings", headers=h).json().get("bookings", []):
                if b["date"] == TEST_DATE:
                    client.delete(f"{API}/bookings/{b['id']}", headers=h)

    def test_day_detail(self, client):
        r = client.get(f"{API}/days/{TEST_DATE}")
        assert r.status_code == 200
        d = r.json()
        assert d["bookable"] is True
        assert len(d["slots"]) == 16

    def test_invalid_date(self, client):
        assert client.get(f"{API}/days/not-a-date").status_code == 400

    def test_anonymous_booking(self, client):
        r = client.post(f"{API}/bookings", json={"date": TEST_DATE, "slot_index": 0,
                                                 "municipality": "TEST_Anon Gemeinde", "note": "TEST"})
        assert r.status_code == 200, r.text
        b = r.json()
        self.ids.append(b["id"])
        assert b["municipality"] == "TEST_Anon Gemeinde"
        assert b["gemeinde_id"] is None
        # verify persisted
        d = client.get(f"{API}/days/{TEST_DATE}").json()
        slot0 = next(s for s in d["slots"] if s["index"] == 0)
        assert slot0["booked"] is True
        assert slot0["booking"]["municipality"] == "TEST_Anon Gemeinde"

    def test_duplicate_slot_conflict(self, client):
        r = client.post(f"{API}/bookings", json={"date": TEST_DATE, "slot_index": 0,
                                                 "municipality": "TEST_Other"})
        assert r.status_code == 409, r.text

    def test_missing_municipality(self, client):
        r = client.post(f"{API}/bookings", json={"date": TEST_DATE, "slot_index": 5})
        assert r.status_code == 400

    def test_invalid_slot(self, client):
        assert client.post(f"{API}/bookings", json={"date": TEST_DATE, "slot_index": 99,
                                                    "municipality": "TEST_X"}).status_code == 400

    def test_holiday_not_bookable(self, client):
        r = client.post(f"{API}/bookings", json={"date": "2026-10-07", "slot_index": 0,
                                                 "municipality": "TEST_X"})
        assert r.status_code == 400

    def test_gemeinde_booking_overrides_municipality(self, client, gemeinde_headers):
        r = client.post(f"{API}/bookings", headers=gemeinde_headers,
                        json={"date": TEST_DATE, "slot_index": 1,
                              "municipality": "IGNORED_NAME", "note": "TEST gem"})
        assert r.status_code == 200, r.text
        b = r.json()
        self.ids.append(b["id"])
        assert b["municipality"] == "Schule Waldstatt"
        assert b["gemeinde_id"]
        # appears in gemeinde bookings
        gb = client.get(f"{API}/gemeinde/bookings", headers=gemeinde_headers).json()["bookings"]
        match = [x for x in gb if x["id"] == b["id"]]
        assert len(match) == 1
        assert match[0]["kid_number"] == 2
        assert match[0]["mpa_start"] == "08:30"  # slot 1 shares pair 0 times
        assert match[0]["arzt_start"] == "08:45"

    def test_batch_booking(self, client):
        r = client.post(f"{API}/bookings/batch", json={"date": TEST_DATE, "slot_indices": [4, 5, 6],
                                                       "municipality": "TEST_Batch Gemeinde", "note": "TEST batch"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert len(d["created"]) == 3
        assert d["failed"] == []
        for b in d["created"]:
            self.ids.append(b["id"])
            assert b["municipality"] == "TEST_Batch Gemeinde"
        # persisted
        day = client.get(f"{API}/days/{TEST_DATE}").json()
        for i in (4, 5, 6):
            assert next(s for s in day["slots"] if s["index"] == i)["booked"] is True

    def test_batch_partial_failure(self, client):
        r = client.post(f"{API}/bookings/batch", json={"date": TEST_DATE, "slot_indices": [4, 7],
                                                       "municipality": "TEST_Batch2"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert len(d["created"]) == 1 and d["created"][0]["slot_index"] == 7
        self.ids.append(d["created"][0]["id"])
        assert len(d["failed"]) == 1
        assert d["failed"][0]["slot_index"] == 4
        assert d["failed"][0]["reason"]

    def test_batch_empty(self, client):
        assert client.post(f"{API}/bookings/batch", json={"date": TEST_DATE, "slot_indices": [],
                                                          "municipality": "TEST_X"}).status_code == 400

    def test_batch_invalid_slot(self, client):
        assert client.post(f"{API}/bookings/batch", json={"date": TEST_DATE, "slot_indices": [1, 50],
                                                          "municipality": "TEST_X"}).status_code == 400

    def test_batch_as_gemeinde(self, client, gemeinde_headers):
        r = client.post(f"{API}/bookings/batch", headers=gemeinde_headers,
                        json={"date": TEST_DATE, "slot_indices": [10, 11], "municipality": "IGNORED"})
        assert r.status_code == 200, r.text
        created = r.json()["created"]
        assert len(created) == 2
        for b in created:
            self.ids.append(b["id"])
            assert b["municipality"] == "Schule Waldstatt"
            assert b["gemeinde_id"]

    def test_admin_update_and_delete_booking(self, client, admin_headers):
        r = client.post(f"{API}/bookings", json={"date": TEST_DATE, "slot_index": 15,
                                                 "municipality": "TEST_ToEdit"})
        assert r.status_code == 200
        bid = r.json()["id"]
        ur = client.put(f"{API}/bookings/{bid}", headers=admin_headers, json={"municipality": "TEST_Edited"})
        assert ur.status_code == 200
        assert ur.json()["municipality"] == "TEST_Edited"
        assert "_id" not in ur.json()
        day = client.get(f"{API}/days/{TEST_DATE}").json()
        assert next(s for s in day["slots"] if s["index"] == 15)["booking"]["municipality"] == "TEST_Edited"
        assert client.delete(f"{API}/bookings/{bid}", headers=admin_headers).status_code == 200
        assert client.delete(f"{API}/bookings/{bid}", headers=admin_headers).status_code == 404
