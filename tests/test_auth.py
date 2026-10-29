"""Account creation, sign-in, sessions, CSRF and lockout."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.main import app
from tests.conftest import TEST_EMAIL, TEST_PASSWORD, login, register_and_login


class TestAuthStatus:
    def test_status_unauthenticated(self, anon_client):
        """Unauthenticated browsers see authenticated: false."""
        response = anon_client.get("/api/auth/status")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["authenticated"] is False
        assert body["user"] is None
        assert body["registration_enabled"] is True

    def test_status_after_registration(self, anon_client):
        """Registered browsers see authenticated: true with user payload."""
        response = anon_client.post(
            "/api/auth/register",
            json={"email": "status@example.test", "password": "a long enough password", "display_name": "StatusUser"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["email"] == "status@example.test"

        status = anon_client.get("/api/auth/status").json()
        assert status["authenticated"] is True
        assert status["user"]["email"] == "status@example.test"
        assert status["registration_enabled"] is True

    def test_status_after_login(self, anon_client):
        """Signed-in browsers see authenticated: true."""
        register_and_login(anon_client, "person@example.test", "a long enough password")

        status = anon_client.get("/api/auth/status").json()
        assert status["authenticated"] is True
        assert status["user"]["email"] == "person@example.test"

    def test_status_after_logout(self, client):
        """After logout, browsers see authenticated: false."""
        # First ensure we're signed in
        register_and_login(client, "logout@example.test", "a long enough password")

        # Check status while signed in
        status = client.get("/api/auth/status").json()
        assert status["authenticated"] is True

        # Log out
        response = client.post("/api/auth/logout")
        assert response.status_code == 200

        # Check status after logout
        status = client.get("/api/auth/status").json()
        assert status["authenticated"] is False
        assert status["user"] is None


class TestRegistration:
    def test_register_creates_an_account_and_signs_in(self, anon_client):
        response = anon_client.post(
            "/api/auth/register",
            json={"email": "new@example.test", "password": "a long enough password", "display_name": "New"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["email"] == "new@example.test"
        assert body["user"]["display_name"] == "New"
        assert body["csrf_token"]

        status = anon_client.get("/api/auth/status").json()
        assert status["authenticated"] is True
        assert status["user"]["email"] == "new@example.test"

    def test_duplicate_email_is_refused(self, client):
        response = client.post(
            "/api/auth/register",
            json={"email": TEST_EMAIL, "password": "another long password"},
        )
        assert response.status_code == 409

    def test_short_password_is_refused(self, anon_client):
        response = anon_client.post(
            "/api/auth/register",
            json={"email": "short@example.test", "password": "short"},
        )
        assert response.status_code == 422

    def test_invalid_email_is_refused(self, anon_client):
        response = anon_client.post(
            "/api/auth/register",
            json={"email": "not-an-email", "password": "a long enough password"},
        )
        assert response.status_code == 422


class TestLogin:
    def test_wrong_password_is_generic_and_unauthorized(self, anon_client):
        register_and_login(anon_client, "person@example.test", "a long enough password")
        anon_client.post("/api/auth/logout")
        response = anon_client.post(
            "/api/auth/login",
            json={"email": "person@example.test", "password": "wrong password"},
        )
        assert response.status_code == 401
        assert "incorrect" in response.json()["detail"].lower()

    def test_unknown_email_looks_the_same_as_a_wrong_password(self, anon_client):
        response = anon_client.post(
            "/api/auth/login",
            json={"email": "nobody@example.test", "password": "whatever long"},
        )
        assert response.status_code == 401
        assert "incorrect" in response.json()["detail"].lower()

    def test_repeated_failures_lock_the_account(self, anon_client):
        register_and_login(anon_client, "lock@example.test", "a long enough password")
        anon_client.post("/api/auth/logout")

        for _ in range(5):
            response = anon_client.post(
                "/api/auth/login",
                json={"email": "lock@example.test", "password": "definitely wrong"},
            )
            assert response.status_code == 401

        response = anon_client.post(
            "/api/auth/login",
            json={"email": "lock@example.test", "password": "a long enough password"},
        )
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    def test_successful_login_restores_access(self, anon_client):
        register_and_login(anon_client, "again@example.test", "a long enough password")
        anon_client.post("/api/auth/logout")
        assert anon_client.get("/api/analytics/summary").status_code == 401

        body = login(anon_client, "again@example.test", "a long enough password")
        assert body["user"]["email"] == "again@example.test"
        assert anon_client.get("/api/analytics/summary").status_code == 200


class TestSessions:
    def test_protected_endpoints_require_sign_in(self, anon_client):
        for path in (
            "/api/meta",
            "/api/devices",
            "/api/analytics/summary",
            "/api/analytics/usage",
            "/api/billing/tariff",
            "/api/ai/status",
            "/api/ai/forecast",
        ):
            assert anon_client.get(path).status_code == 401, path

    def test_logout_revokes_the_session(self, client):
        assert client.get("/api/auth/me").status_code == 200
        response = client.post("/api/auth/logout")
        assert response.status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/analytics/summary").status_code == 401

    def test_cookie_writes_need_the_csrf_header(self, anon_client):
        body = register_and_login(anon_client, "csrf@example.test", "a long enough password")
        assert body["csrf_token"]
        anon_client.headers.pop("X-CSRF-Token", None)

        response = anon_client.post("/api/devices", json={"id": "csrf-device"})
        assert response.status_code == 403

        anon_client.headers.update({"X-CSRF-Token": body["csrf_token"]})
        response = anon_client.post("/api/devices", json={"id": "csrf-device"})
        assert response.status_code == 200

    def test_password_change_requires_the_current_password(self, client):
        response = client.post(
            "/api/auth/password",
            json={"current_password": "not the password", "new_password": "a different long password"},
        )
        assert response.status_code == 403

        response = client.post(
            "/api/auth/password",
            json={"current_password": TEST_PASSWORD, "new_password": "a different long password"},
        )
        assert response.status_code == 200

    def test_password_change_invalidates_other_sessions(self, anon_client):
        register_and_login(anon_client, "rotate@example.test", "a long enough password")

        other = TestClient(app)
        try:
            login(other, "rotate@example.test", "a long enough password")
            assert other.get("/api/auth/me").status_code == 200

            response = anon_client.post(
                "/api/auth/password",
                json={
                    "current_password": "a long enough password",
                    "new_password": "an even longer password",
                },
            )
            assert response.status_code == 200

            # The changing session survives; the other one is revoked.
            assert anon_client.get("/api/auth/me").status_code == 200
            assert other.get("/api/auth/me").status_code == 401
        finally:
            other.close()

    def test_a_bogus_session_cookie_is_not_accepted(self, anon_client):
        anon_client.cookies.set("wattxpense_session", "not-a-real-token")
        assert anon_client.get("/api/auth/me").status_code == 401
