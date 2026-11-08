import unittest
from pathlib import Path

from app.main import app


class DashboardRouteTests(unittest.TestCase):
    def test_v24_dashboard_route_is_registered(self):
        routes = {route.path for route in app.routes if hasattr(route, "path")}
        self.assertIn("/dashboard", routes)

    def test_v3_validation_dashboard_and_assets_are_available(self):
        routes = {route.path for route in app.routes if hasattr(route, "path")}
        self.assertIn("/v3", routes)

        static = Path(__file__).resolve().parents[1] / "app" / "static"
        html = (static / "v3.html").read_text(encoding="utf-8")
        javascript = (static / "v3.js").read_text(encoding="utf-8")

        for section in ("rules", "alerts", "notifications", "screener", "saved"):
            self.assertIn(f'data-page="{section}"', html)
        for endpoint in (
            "/api/v1/rules/test/",
            "/api/v1/alerts",
            "/api/v1/notifications",
            "/api/v1/screener/run",
            "/api/v1/screeners",
        ):
            self.assertIn(endpoint, javascript)


if __name__ == "__main__":
    unittest.main()
