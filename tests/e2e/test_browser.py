"""The browser app, driven in Chromium against a real server filled with scripts/demo-data.

These catch what the Python tests cannot: timing between views, focus, and one person's page state
surviving into the next person's session. They need Playwright with its Chromium
(`.venv/bin/pip install -e '.[browser-tests]' && .venv/bin/playwright install chromium`),
GEOLOGS_TEST_DATABASE_URL (scripts/test-db), scripts/demo-data, and internet access for the map
libraries and tiles. Without any of those they are skipped. Run only them with:

    .venv/bin/python -m unittest discover -s tests -p 'test_browser.py' -v
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
DEMO_DATA = ROOT / "scripts" / "demo-data"
# Not geologs_test_…: the Python tests drop those when they start.
DATABASE = "geologs_browser"
TOKEN = "browser-test-token"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

_skip = (
    "Playwright is not installed" if sync_playwright is None
    else "GEOLOGS_TEST_DATABASE_URL is not set" if not os.environ.get("GEOLOGS_TEST_DATABASE_URL")
    else "scripts/demo-data is missing" if not DEMO_DATA.exists()
    else ""
)

server = None
base_url = ""
playwright = None
browser = None


def _admin():
    import psycopg
    return psycopg.connect(os.environ["GEOLOGS_TEST_DATABASE_URL"], autocommit=True)


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def setUpModule():
    global server, base_url, playwright, browser
    if _skip:
        raise unittest.SkipTest(_skip)
    with _admin() as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{DATABASE}"')
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    # Only what the test server needs: never the checkout's .env, with its real keys.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GEOLOGS_ENV_FILE": os.devnull,
        "GEOLOGS_DATABASE_URL": urlunsplit(urlsplit(os.environ["GEOLOGS_TEST_DATABASE_URL"])._replace(path="/" + DATABASE)),
        "GEOLOGS_APP_DB_PASSWORD": "geologs-test-app",
        "GEOLOGS_TOKEN": TOKEN,
        "GEOLOGS_TIMEZONE": "America/Los_Angeles",
        "GEOLOGS_PUBLIC_URL": base_url,
    }
    server = subprocess.Popen([sys.executable, "-m", "geologs", "serve", "--port", str(port)], env=env, cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen(base_url, timeout=1)
            break
        except OSError:
            if server.poll() is not None:
                raise RuntimeError("The GeoLogs server exited while starting")
            time.sleep(0.5)
    else:
        raise RuntimeError("The GeoLogs server did not start")
    subprocess.run([sys.executable, str(DEMO_DATA), "--url", base_url, "--token", TOKEN, "--days", "45"],
                   check=True, stdout=subprocess.DEVNULL)
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch()


def tearDownModule():
    if browser:
        browser.close()
    if playwright:
        playwright.stop()
    if server:
        server.terminate()
        server.wait(10)
        with _admin() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')


def recent_weekdays(count):
    """Weekdays before today, newest first: ordinary demo days at home, the studio and the café."""
    day, found = date.today() - timedelta(days=1), []
    while len(found) < count:
        if day.weekday() < 5:
            found.append(day.isoformat())
        day -= timedelta(days=1)
    return found


# Every pin on the Home map, against the map's edges and the glass cards over it.
PINS_IN_THE_CLEAR = """() => {
  const visible = (el) => el && !el.hidden && el.getBoundingClientRect().width ? el.getBoundingClientRect() : null;
  const cards = [visible(document.querySelector('.home-stack')), visible(document.getElementById('home-brief'))].filter(Boolean);
  const box = document.getElementById('map').getBoundingClientRect();
  const problems = [];
  for (const pin of document.querySelectorAll('#map .leaflet-marker-icon')) {
    const b = pin.getBoundingClientRect();
    if (!b.width) continue;
    if (b.left < box.left || b.right > box.right || b.top < box.top || b.bottom > box.bottom) problems.push('off the map');
    for (const c of cards) if (b.left < c.right && b.right > c.left && b.top < c.bottom && b.bottom > c.top) problems.push('under a card');
  }
  return problems;
}"""


class BrowserTest(unittest.TestCase):
    def setUp(self):
        self.context = browser.new_context(viewport={"width": 1440, "height": 960})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(f"page error: {error}"))
        self.page.on("response", lambda response: response.status >= 500 and response.url.startswith(base_url)
                     and self.errors.append(f"HTTP {response.status} from {response.url}"))

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def api(self, path, body):
        """POST as the server's owner, outside any browser session."""
        request = urllib.request.Request(base_url + path, data=json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(request))

    def open_sign_in(self):
        self.page.goto(base_url)
        self.page.wait_for_selector("#auth-panel:not([hidden])")

    def sign_in_with_token(self):
        self.open_sign_in()
        self.page.locator('[data-auth-go="token"]:visible').first.click()
        self.page.fill("#auth-token", TOKEN)
        self.page.click("#auth-form button.primary")
        self.page.wait_for_selector("#auth-panel", state="hidden")

    def sign_in_with_password(self, email, password):
        if self.page.is_visible("#auth-token"):
            self.page.locator('[data-auth-go="signin"]:visible').first.click()
        self.page.fill("#auth-email", email)
        self.page.fill("#auth-password", password)
        self.page.click("#auth-signin button.primary")
        self.page.wait_for_selector("#auth-panel", state="hidden")

    def add_user(self, email, name, password):
        invite = self.api("/api/invites", {"email": email, "role": "user"})
        with urllib.request.urlopen(urllib.request.Request(
                base_url + "/api/invites/accept", headers={"Content-Type": "application/json"},
                data=json.dumps({"token": invite["token"], "password": password, "name": name}).encode())):
            pass


class SignInTest(BrowserTest):
    def test_the_cursor_stays_in_the_password_while_the_page_polls(self):
        self.open_sign_in()
        self.page.click("#auth-email")
        self.page.keyboard.type("alex@example.com")
        self.page.click("#auth-password")
        self.page.keyboard.type("hunter")
        # The page asks the server again every 10 s, and each refusal used to reopen the screen.
        self.page.wait_for_timeout(12000)
        self.page.keyboard.type("22")
        self.assertEqual(self.page.evaluate("document.activeElement.id"), "auth-password")
        self.assertEqual(self.page.input_value("#auth-email"), "alex@example.com")
        self.assertEqual(self.page.input_value("#auth-password"), "hunter22")

    def test_the_token_signs_in(self):
        self.sign_in_with_token()
        self.assertTrue(self.page.is_visible("#home-view"))


class SwitchingPeopleTest(BrowserTest):
    def test_nothing_of_the_last_person_is_left_for_the_next(self):
        self.api("/api/account", {"name": "Olivia Owner"})
        self.add_user("bea@example.com", "Bea User", "bea-password-123")
        self.sign_in_with_token()
        self.page.click('.nav[data-view="settings"]')
        self.page.click('[data-settings-page="settings-admin-users"]')
        self.page.click("#auth-button")
        self.page.wait_for_function("document.getElementById('account-name').value === 'Olivia Owner'")

        self.page.click("#account-sign-out")
        self.page.wait_for_selector("#auth-panel:not([hidden])")
        self.assertIn("signed out", self.page.text_content("#auth-panel").lower())
        self.assertEqual(self.page.input_value("#account-name"), "")
        self.assertEqual(self.page.inner_html("#account-sessions"), "")

        self.sign_in_with_password("bea@example.com", "bea-password-123")
        self.page.click("#auth-button")
        self.page.wait_for_function("document.getElementById('account-email').textContent === 'bea@example.com'")
        self.assertEqual(self.page.input_value("#account-name"), "Bea User")
        self.page.click('.nav[data-view="settings"]')
        self.assertTrue(self.page.evaluate("document.querySelector('.settings-group[data-group=admin]').hidden"))
        self.assertNotEqual(self.page.evaluate("document.querySelector('.settings-section:not([hidden])').id"),
                            "settings-admin-users")


class HomeMapTest(BrowserTest):
    def setUp(self):
        super().setUp()
        self.sign_in_with_token()

    def test_each_day_fits_clear_of_the_cards(self):
        for day in recent_weekdays(4):
            with self.subTest(day=day):
                self.page.goto(f"{base_url}/#date={day}")
                self.page.wait_for_timeout(3500)
                self.assertEqual(self.page.evaluate(PINS_IN_THE_CLEAR), [])

    def test_stepping_through_days_quickly_ends_fitted_to_the_last(self):
        start = recent_weekdays(1)[0]
        self.page.goto(f"{base_url}/#date={start}")
        self.page.wait_for_timeout(3000)
        for _ in range(5):
            self.page.keyboard.press("ArrowLeft")
            self.page.wait_for_timeout(250)
        self.page.wait_for_timeout(3500)
        self.assertEqual(self.page.input_value("#day-date"), (date.fromisoformat(start) - timedelta(days=5)).isoformat())
        self.assertEqual(self.page.evaluate(PINS_IN_THE_CLEAR), [])


class ReplayTest(BrowserTest):
    def setUp(self):
        super().setUp()
        self.sign_in_with_token()

    def test_a_day_replays_and_space_pauses_it_without_closing(self):
        self.page.goto(f"{base_url}/#date={recent_weekdays(1)[0]}")
        self.page.wait_for_selector("#replay-day:not([hidden])")
        self.page.click("#replay-day")
        self.page.wait_for_selector("#replay-dialog[open]")
        # It starts on its own, a moment after fitting the day.
        self.page.wait_for_function("document.getElementById('replay-play').ariaLabel === 'Pause'", timeout=5000)
        self.page.keyboard.press(" ")
        self.assertEqual(self.page.get_attribute("#replay-play", "aria-label"), "Play")
        self.assertTrue(self.page.evaluate("document.getElementById('replay-dialog').open"))
        # At the end, the day's whole distance has been travelled.
        self.page.locator("#replay-scrub").evaluate("(scrub) => { scrub.value = 1000; scrub.dispatchEvent(new Event('input')); }")
        self.assertEqual(self.page.text_content("#replay-distance"),
                         self.page.evaluate("document.getElementById('replay-summary').textContent.split(' · ')[0]"))
        self.page.keyboard.press("Escape")
        self.assertFalse(self.page.evaluate("document.getElementById('replay-dialog').open"))


class ViewsTest(BrowserTest):
    def test_every_view_opens(self):
        self.sign_in_with_token()
        self.page.click('.nav[data-view="places"]')
        self.page.wait_for_selector("#all-visited-places .list-item, #all-visited-places li")
        self.assertIn("Pearl District Studio", self.page.text_content("#places-view"))
        self.page.click('.nav[data-view="summaries"]')
        self.page.click('[data-summary-scale="month"]')
        self.page.wait_for_function("/\\d+ days? recorded/.test(document.getElementById('summaries-period').textContent)")
        self.page.click('[data-summary-scale="year"]')
        self.page.wait_for_timeout(1500)
        self.page.click('[data-summary-scale="day"]')
        self.page.click('.nav[data-view="reminders"]')
        self.page.wait_for_function("document.getElementById('reminders-view').textContent.includes('Pick up oat milk')")
        self.page.click('.nav[data-view="settings"]')
        self.page.wait_for_timeout(500)
        self.assertTrue(re.search(r"Units", self.page.text_content("#settings-view")))


class DataTest(BrowserTest):
    def setUp(self):
        super().setUp()
        self.sign_in_with_token()
        self.page.click('.nav[data-view="settings"]')
        self.page.click('[data-settings-page="settings-data"]')

    def test_a_range_downloads_as_csv(self):
        day = recent_weekdays(1)[0]
        self.page.fill("#export-start", day)
        self.page.fill("#export-end", day)
        with self.page.expect_download() as download:
            self.page.click('[data-export="csv"]')
        rows = Path(download.value.path()).read_text().splitlines()
        self.assertEqual(rows[0], "timestamp,lat,lon,accuracy_m,speed_mps,source")
        self.assertGreater(len(rows), 50)
        self.assertTrue(all(row.endswith(",owntracks:alex/phone") for row in rows[1:]))

    def test_a_file_imports_and_says_what_it_added(self):
        # Long ago, so the imported fixes stay off the recent days the other tests look at.
        path = Path(self.id().rsplit(".", 1)[-1] + ".csv").resolve()
        path.write_text("timestamp,lat,lon\n2020-01-01T10:00:00Z,45.5,-122.6\n2020-01-01T10:05:00Z,45.51,-122.6\n")
        self.addCleanup(path.unlink)
        self.page.set_input_files("#import-file", str(path))
        self.page.fill("#import-source", "old-gps")
        self.page.click("#import-go")
        self.page.wait_for_function("document.getElementById('import-status').textContent.includes('Imported')", timeout=60000)
        self.assertIn("2 new locations", self.page.text_content("#import-status"))
        self.assertIn("old-gps", self.page.text_content("#import-status"))

    def test_a_full_backup_downloads_and_imports_back_as_already_here(self):
        with self.page.expect_download() as download:
            self.page.click("#backup-go")
        self.assertRegex(download.value.suggested_filename, r"^geologs-backup-\d{4}-\d\d-\d\d\.json\.gz$")
        self.page.set_input_files("#import-file", download.value.path())
        self.page.click("#import-go")
        self.page.wait_for_function("document.getElementById('import-status').textContent.includes('Restored')", timeout=60000)
        self.assertIn("0 new locations", self.page.text_content("#import-status"))
        self.assertIn("already here", self.page.text_content("#import-status"))


class QuietTrackerTest(BrowserTest):
    def test_a_tracker_can_be_watched_from_its_row(self):
        self.sign_in_with_token()
        self.page.click('.nav[data-view="settings"]')
        self.page.click('[data-settings-page="settings-sources"]')
        select = self.page.locator('#priority-list li[data-source="owntracks:alex/phone"] [data-quiet]')
        select.select_option("6")
        self.page.wait_for_function("document.body.textContent.includes(\"sends nothing for 6 h\")")
        with urllib.request.urlopen(urllib.request.Request(base_url + "/api/sources", headers={"Authorization": "Bearer " + TOKEN})) as answer:
            self.assertEqual(json.load(answer)["quiet_alerts"]["owntracks:alex/phone"]["hours"], 6)
        select.select_option("0")


class OfflineTest(BrowserTest):
    def test_it_installs_and_opens_offline_with_the_last_day_then_forgets_it_on_sign_out(self):
        manifest = json.load(urllib.request.urlopen(base_url + "/manifest.webmanifest"))
        self.assertEqual((manifest["display"], manifest["start_url"]), ("standalone", "/"))
        day = recent_weekdays(1)[0]
        self.sign_in_with_token()
        self.page.evaluate("navigator.serviceWorker.ready.then(() => true)")
        # Once the worker looks after the page, a visit keeps the app and the day.
        self.page.reload()
        self.page.wait_for_function("navigator.serviceWorker.controller !== null")
        self.page.goto(f"{base_url}/#date={day}")
        self.page.wait_for_timeout(4000)
        self.context.set_offline(True)
        self.page.reload()
        self.page.wait_for_function("document.getElementById('connection').textContent === 'Offline'", timeout=20000)
        self.assertEqual(self.page.input_value("#day-date"), day)
        self.page.wait_for_function("document.querySelectorAll('#map .leaflet-marker-icon').length > 0", timeout=20000)
        self.context.set_offline(False)
        self.page.click("#auth-button")
        self.page.click("#account-sign-out")
        self.page.wait_for_selector("#auth-panel:not([hidden])")
        self.assertFalse(self.page.evaluate("caches.has('geologs-data-v1')"))


if __name__ == "__main__":
    unittest.main()
