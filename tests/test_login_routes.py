"""Exercise production login handlers without connecting to guest data."""
import ast
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import Mock

from flask import Flask, abort, flash, jsonify, redirect, request, session, url_for
from login_security import safe_login_destination


class LoginRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test-only"
        app.add_url_rule("/admin", "admin", lambda: abort(401))
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {
            "id": 1, "username": "test-staff", "role": "host", "password_hash": "test-only"}
        scope = dict(app=app, request=request, session=session, redirect=redirect,
                     url_for=url_for, jsonify=jsonify, flash=flash, datetime=datetime,
                     safe_login_destination=safe_login_destination,
                     PUBLIC_BASE_URL="https://reservations.sienaatl.com", APP_NAME="Siena",
                     db=lambda: conn, get_client_ip=lambda: "127.0.0.1",
                     is_login_rate_limited=lambda *a: False,
                     record_login_attempt=Mock(), audit=Mock(),
                     check_password_hash=lambda *a: True,
                     render_template=lambda *a, **kw: "Siena login")
        source = ast.parse((Path(__file__).parents[1] / "app.py").read_text())
        handlers = {"handle_unauthorized", "staff_login", "protect_staff_login"}
        module = ast.Module(body=[n for n in source.body
                                  if isinstance(n, ast.FunctionDef) and n.name in handlers],
                            type_ignores=[])
        exec(compile(module, "app.py", "exec"), scope)
        self.client = app.test_client()

    def test_unauthenticated_redirect_has_relative_return_path(self):
        response = self.client.get("/admin?date=2026-09-25")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/staff/login?next=/admin?date%3D2026-09-25")

    def test_successful_login_cannot_redirect_off_site(self):
        for destination in ("https://example.com/collect", "//example.com", "/%5cexample.com"):
            response = self.client.post("/staff/login", query_string={"next": destination},
                                        data={"username": "test-staff", "password": "test-only"})
            self.assertEqual(response.headers["Location"], "/admin")

    def test_successful_login_keeps_dashboard_filters(self):
        response = self.client.post("/staff/login", query_string={"next": "/admin?date=2026-09-25"},
                                    data={"username": "test-staff", "password": "test-only"})
        self.assertEqual(response.headers["Location"], "/admin?date=2026-09-25")

    def test_login_response_restricts_forms_and_caching(self):
        response = self.client.get("/staff/login")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("form-action 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow")


if __name__ == "__main__":
    unittest.main()
