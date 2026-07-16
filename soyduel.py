"""
Soyduel bot: posts a random KYM-scraped image + quoted arrow reply to a
single thread on soyjak.st on a cooldown.

Config lives in soyduel.toml (path overridable via SOYDUEL_CONFIG env var).
Use soyduelctl.py to start/stop/restart/status this as a background process.
"""
import functools
import json
import os
import random
import re
import sys
import tempfile
import time
import tomllib
from datetime import datetime

from curl_cffi import requests as curl_requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from vncdotool import api as vnc_api

CONFIG_PATH = os.environ.get("SOYDUEL_CONFIG", os.path.join(os.path.dirname(__file__), "soyduel.toml"))

SOYBOORU_API = "https://soybooru.com/api/booru/posts"
POST_ID_RE = re.compile(r'id="(?:op|reply)_(\d+)"')


@functools.lru_cache(maxsize=1)
def load_config(path=None):
    path = path or CONFIG_PATH
    with open(path, "rb") as f:
        return tomllib.load(f)


def thread_url(config):
    return f"https://soyjak.st/{config['target']['board']}/thread/{config['target']['thread_id']}.html"


def get_session():
    return curl_requests.Session(impersonate="chrome")


class VncFallbackBrowser:
    """Drives a Firefox/Chromium already open on a remote host purely via
    raw VNC mouse/keyboard input -- no WebDriver, no DOM access. Click/type
    coordinates are fractions of resolution, matched against a calibration
    profile (see soyduel_calibrate.py)."""

    def __init__(self, config):
        rb = config["remote_browser"]
        server = f"{rb['vnc_host']}::{rb['vnc_port']}"
        self.client = vnc_api.connect(server, password=rb.get("vnc_password") or None)
        self._config = config
        try:
            self.refresh_resolution()
        except Exception:
            self.client.disconnect()
            raise

    def refresh_resolution(self):
        """Re-measure the remote screen and reload/auto-generate the matching
        calibration profile. Resolution can drift mid-session, so call this
        at the start of every posting cycle, not just at startup."""
        rb = self._config["remote_browser"]
        tmp_path = tempfile.mktemp(suffix=".png", prefix="soyduel_res_check_")
        try:
            self.client.captureScreen(tmp_path)
            from PIL import Image
            self.width, self.height = Image.open(tmp_path).size
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        res_key = f"{self.width}x{self.height}"
        profiles = rb["vnc_fallback"].get("profiles", {})
        if res_key not in profiles:
            from soyduel_calibrate import detect_regions, write_profile_to_toml
            self.navigate(thread_url(self._config))
            tmp_path = tempfile.mktemp(suffix=".png", prefix="soyduel_autocal_")
            try:
                self.client.captureScreen(tmp_path)
                profile = detect_regions(tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            if profile["resolution"] != res_key:
                raise RuntimeError(
                    f"Auto-calibration resolution mismatch: measured {res_key} but "
                    f"detect_regions saw {profile['resolution']} -- screen changed mid-capture."
                )
            write_profile_to_toml(dict(profile))
            load_config.cache_clear()
            self._config = load_config()
            rb = self._config["remote_browser"]
            profiles = rb["vnc_fallback"].get("profiles", {})
            if res_key not in profiles:
                raise RuntimeError(f"Auto-calibration for {res_key} didn't persist as expected.")
        self.regions = profiles[res_key]

    def _abs_xy(self, frac_key):
        fx, fy = self.regions[frac_key]
        return int(fx * self.width), int(fy * self.height)

    def click_region(self, frac_key):
        x, y = self._abs_xy(frac_key)
        self.client.mouseMove(x, y)
        self.client.mousePress(1)

    def type_text(self, text):
        for ch in text:
            self.client.keyPress(ch)

    def key(self, key_name):
        self.client.keyPress(key_name)

    _DIALOG_TITLEBAR_COLOR = (139, 175, 219)
    _DIALOG_SIZE = (1125, 774)  # native GTK dialog size; GTK centers it on the whole screen
    _DIALOG_TITLEBAR_OFFSET = (95, 5)
    _DIALOG_ACTION_BUTTON_OFFSET = (1076, 755)

    def _dialog_box(self):
        dw, dh = self._DIALOG_SIZE
        return (self.width - dw) // 2, (self.height - dh) // 2

    def _dialog_titlebar_present(self, tmp_path):
        from PIL import Image
        img = Image.open(tmp_path).convert("RGB")
        left, top = self._dialog_box()
        ox, oy = self._DIALOG_TITLEBAR_OFFSET
        for dy in range(-15, 16, 3):
            px = img.getpixel((left + ox, top + oy + dy))
            if all(abs(a - b) <= 20 for a, b in zip(px, self._DIALOG_TITLEBAR_COLOR)):
                return True
        return False

    def _poll_titlebar(self, want_present, timeout, interval=0.3):
        elapsed = 0.0
        tmp_path = tempfile.mktemp(suffix=".png", prefix="soyduel_dlg_check_")
        try:
            while elapsed < timeout:
                self.client.captureScreen(tmp_path)
                if self._dialog_titlebar_present(tmp_path) == want_present:
                    return True
                time.sleep(interval)
                elapsed += interval
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return False

    def wait_for_dialog(self, timeout=8):
        return self._poll_titlebar(True, timeout)

    def wait_for_dialog_closed(self, timeout=5):
        return self._poll_titlebar(False, timeout)

    def click_dialog_action_button(self):
        left, top = self._dialog_box()
        ox, oy = self._DIALOG_ACTION_BUTTON_OFFSET
        self.client.mouseMove(left + ox, top + oy)
        self.client.mousePress(1)
        return self.wait_for_dialog_closed(timeout=3)

    def navigate(self, url):
        self.client.mouseMove(int(self.width * 0.3), 63)
        self.client.mousePress(1)
        time.sleep(0.3)
        self.key("ctrl-a")
        self.type_text(url)
        self.key("enter")
        time.sleep(2)

    def quit(self):
        self.client.disconnect()
        vnc_api.shutdown()


def get_browser(config):
    remote = config.get("remote_browser", {})
    if remote.get("enabled"):
        if remote.get("webdriver_url"):
            options = Options()
            return webdriver.Remote(command_executor=remote["webdriver_url"], options=options)
        return VncFallbackBrowser(config)

    os.environ.setdefault("DISPLAY", config["browser"]["display"])
    options = Options()
    options.binary_location = config["browser"]["firefox_binary"]
    options.set_preference("browser.download.folderList", 2)
    service = Service(executable_path=config["browser"]["geckodriver_binary"])
    return webdriver.Firefox(options=options, service=service)


def load_seen_ids(config):
    path = config["source"]["seen_images_file"]
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return set(json.load(f))


def save_seen_ids(config, seen_ids):
    path = config["source"]["seen_images_file"]
    with open(path, "w") as f:
        json.dump(sorted(seen_ids), f)


def soybooru_total_pages(session, config):
    r = session.get(SOYBOORU_API, params={"q": config["source"]["soybooru_search_query"], "page": 1})
    r.raise_for_status()
    return r.json()["totalPages"]


def pick_unique_post(session, config, seen_ids):
    total_pages = soybooru_total_pages(session, config)
    pages = list(range(1, total_pages + 1))
    random.shuffle(pages)

    for page in pages:
        r = session.get(
            SOYBOORU_API,
            params={"q": config["source"]["soybooru_search_query"], "page": page},
        )
        if r.status_code != 200:
            continue
        posts = r.json().get("posts", [])
        random.shuffle(posts)
        for post in posts:
            if not post["mimeType"].startswith("image/"):  # video posts break the VNC-fallback flow
                continue
            if post["id"] not in seen_ids:
                return post
    raise RuntimeError("No unposted images left matching the SoyBooru query")


def direct_image_url(post):
    return f"{SOYBOORU_API}/{post['id']}/file"


def download_post_image_to_tempfile(session, post):
    r = session.get(direct_image_url(post))
    r.raise_for_status()
    ext = post["mimeType"].split("/")[-1]
    fd, path = tempfile.mkstemp(suffix=f".{ext}", prefix="soyduel_")
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    return path


def latest_post_id(session, config):
    r = session.get(thread_url(config))
    r.raise_for_status()
    ids = POST_ID_RE.findall(r.text)
    if not ids:
        raise RuntimeError("Couldn't find any post IDs in thread")
    return ids[-1]


def duel_body(quote_id):
    arrows = ">" * random.randint(1, 6)
    return f">>{quote_id}\n{arrows}soy"


def post_reply_webdriver(browser, session, config, image_path):
    quote_id = latest_post_id(session, config)
    url = thread_url(config)

    browser.get(url)
    browser.execute_script("localStorage.setItem('file_dragdrop', 'false');")
    browser.get(url)

    body = browser.find_element(By.NAME, "body")
    body.clear()
    body.send_keys(duel_body(quote_id))

    file_input = browser.find_element(By.NAME, "file")
    file_input.send_keys(image_path)

    time.sleep(5)

    submit = browser.find_element(By.CSS_SELECTOR, 'input[name="post"]')
    submit.click()

    time.sleep(3)
    return browser.current_url


def post_reply_vnc_fallback(browser, session, config, post):
    quote_id = latest_post_id(session, config)
    url = thread_url(config)

    # No channel to hand the remote machine binary bytes directly, so have the
    # remote browser fetch the image itself and save it locally via the
    # native GTK "Save As" dialog, then reference that path.
    ext = post["mimeType"].split("/")[-1]
    remote_image_path = f"/tmp/soyduel_upload_{post['id']}_{int(time.time())}.{ext}"

    browser.navigate(direct_image_url(post))
    browser.key("ctrl-s")
    if not browser.wait_for_dialog():
        raise RuntimeError("Save dialog never appeared after Ctrl+S")
    browser.key("ctrl-l")
    time.sleep(0.3)
    browser.key("ctrl-a")
    browser.type_text(remote_image_path)
    browser.key("enter")
    time.sleep(0.3)
    if not browser.click_dialog_action_button():
        raise RuntimeError("Save dialog didn't close after clicking Save")

    browser.navigate(url)

    browser.click_region("comment_field")
    body = duel_body(quote_id)
    for i, line in enumerate(body.split("\n")):
        if i > 0:
            browser.key("enter")
        browser.type_text(line)

    browser.click_region("file_browse_button")
    if not browser.wait_for_dialog():
        raise RuntimeError("Open dialog never appeared after clicking the file dropzone")

    browser.key("ctrl-l")
    time.sleep(0.3)
    browser.type_text(remote_image_path)
    browser.key("enter")
    time.sleep(0.3)
    if not browser.click_dialog_action_button():
        raise RuntimeError("Open dialog didn't close after clicking Open")

    time.sleep(5)

    browser.click_region("post_button")
    time.sleep(3)
    return url  # no DOM access in this mode to confirm the actual result


def post_reply(browser, session, config, post):
    if isinstance(browser, VncFallbackBrowser):
        return post_reply_vnc_fallback(browser, session, config, post)

    image_path = download_post_image_to_tempfile(session, post)
    try:
        return post_reply_webdriver(browser, session, config, image_path)
    finally:
        os.remove(image_path)


def run(max_posts=None, config=None):
    config = config or load_config()
    cooldown = config["timing"]["cooldown_seconds"]

    session = get_session()
    browser = get_browser(config)
    seen_ids = load_seen_ids(config)
    posted = 0
    try:
        while max_posts is None or posted < max_posts:
            try:
                if isinstance(browser, VncFallbackBrowser):
                    browser.refresh_resolution()
                post = pick_unique_post(session, config, seen_ids)
                result_url = post_reply(browser, session, config, post)
                seen_ids.add(post["id"])
                save_seen_ids(config, seen_ids)
                posted += 1
                print(f"[{datetime.now().isoformat(timespec='seconds')}] "
                      f"post {posted}: soybooru#{post['id']} -> {result_url}", flush=True)
            except Exception as exc:
                print(f"[{datetime.now().isoformat(timespec='seconds')}] error: {exc}", flush=True)

            time.sleep(cooldown)
    finally:
        browser.quit()


if __name__ == "__main__":
    max_posts = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(max_posts=max_posts)
