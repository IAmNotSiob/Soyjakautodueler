"""
Auto-calibrates VNC-fallback click coordinates for a remote machine using
PIL-based image analysis instead of manual eyeballing. Screenshots the live
thread's reply form and locates form elements by pixel color.

Usage:
    python3 soyduel_calibrate.py 
    python3 soyduel_calibrate.py  --password foo --write

Without --write, it just prints the detected profile. With --write, it saves
the profile into soyduel.toml under [remote_browser.vnc_fallback.profiles.WxH].
"""
import argparse
import os
import re
import time

import tomllib
from PIL import Image
from vncdotool import api as vnc_api

HERE = os.path.dirname(os.path.abspath(__file__))
TOML_PATH = os.path.join(HERE, "soyduel.toml")

LABEL_PURPLE = (153, 136, 238)
BUTTON_GRAY = (233, 233, 237)
PAGE_BG = (238, 242, 255)
WHITE = (255, 255, 255)


def _close(c1, c2, tol=30):
    return all(abs(a - b) <= tol for a, b in zip(c1, c2))


def capture_calibration_screenshot(vnc_host, vnc_port, thread_url, password=None, out_path="/tmp/soyduel_calib.png"):
    client = vnc_api.connect(f"{vnc_host}::{vnc_port}", password=password)
    try:
        client.keyPress("esc")
        time.sleep(0.3)
        # Click the address bar directly -- Ctrl+L doesn't reliably focus it over VNC.
        client.captureScreen(out_path)
        probe = Image.open(out_path)
        client.mouseMove(int(probe.width * 0.3), 63)
        client.mousePress(1)
        time.sleep(0.3)
        client.keyPress("ctrl-a")
        for ch in thread_url:
            client.keyPress(ch)
        client.keyPress("enter")
        time.sleep(2)
        client.captureScreen(out_path)
    finally:
        client.disconnect()
    return out_path


def _find_label_column(img, x_step=4, y_step=2, min_rel=0.5):
    """Locate the purple Name/Email/Subject/... label column -- resolution
    independent, since the form's x-position shifts with window width."""
    W, H = img.size
    counts = []
    for x in range(0, W, x_step):
        c = 0
        for y in range(0, H, y_step):
            if _close(img.getpixel((x, y)), LABEL_PURPLE):
                c += 1
        counts.append((x, c))
    max_c = max((c for _, c in counts), default=0)
    if max_c == 0:
        return None
    threshold = max_c * min_rel
    xs = [x for x, c in counts if c >= threshold]
    if not xs:
        return None
    return min(xs), max(xs) + x_step


def _find_label_rows(img, x0=446, x1=553, step=4, threshold=0.3):
    W, H = img.size
    def row_is_label(y):
        count = total = 0
        for x in range(x0, min(x1, W), step):
            total += 1
            if _close(img.getpixel((x, y)), LABEL_PURPLE):
                count += 1
        return total > 0 and count / total > threshold

    bands = []
    in_band = False
    start_y = None
    for y in range(H):
        is_label = row_is_label(y)
        if is_label and not in_band:
            in_band, start_y = True, y
        elif not is_label and in_band:
            in_band = False
            bands.append((start_y, y))
    if in_band:
        bands.append((start_y, H))
    return bands


def _find_white_span(img, y, x_start, x_end, step=2, min_len=20):
    run_start = None
    last_match = None
    for x in range(x_start, x_end, step):
        is_white = _close(img.getpixel((x, y)), WHITE, tol=5)
        if is_white:
            if run_start is None:
                run_start = x
            last_match = x
        elif run_start is not None and x - last_match > 6:
            if last_match - run_start >= min_len:
                return run_start, last_match
            run_start, last_match = None, None
    if run_start is not None and last_match - run_start >= min_len:
        return run_start, last_match
    return None


def _windowed_match(img, x, y, target, radius=3, tol=10):
    for dx in range(-radius, radius + 1):
        if _close(img.getpixel((x + dx, y)), target, tol):
            return True
    return False


def _find_widget_span(img, y, x_start, x_end, step=2, min_gap=8):
    run_start = None
    run_end = None
    gap = 0
    for x in range(x_start, x_end, step):
        if _windowed_match(img, x, y, BUTTON_GRAY):
            if run_start is None:
                run_start = x
            run_end = x
            gap = 0
        elif run_start is not None:
            gap += step
            if gap >= min_gap:
                return run_start, run_end
    if run_start is not None:
        return run_start, run_end
    return None


def detect_regions(png_path):
    img = Image.open(png_path).convert("RGB")
    W, H = img.size

    label_col = _find_label_column(img)
    if not label_col:
        raise RuntimeError(
            "Couldn't locate the purple label column anywhere in the image. "
            "Page may not have rendered as expected."
        )
    label_x0, label_x1 = label_col
    content_x0 = label_x1
    content_x1 = label_x1 + 450

    bands = _find_label_rows(img, x0=label_x0, x1=label_x1)
    # name/email/subject/comment/flag are always the first 5 rows. What
    # follows varies (an extra "Select" row may or may not precede File).
    if len(bands) < 6:
        raise RuntimeError(
            f"Expected at least 6 form rows (name/email/subject/comment/flag/file), "
            f"found {len(bands)}: {bands}. Page may not have rendered as expected."
        )
    row_names = ["name", "email", "subject", "comment", "flag"]
    rows = dict(zip(row_names, bands[:5]))
    trailing = bands[5:]
    # The last band can be truncated by a short screenshot, shrinking its
    # measured height below a plain row's -- so pick by height normally, but
    # if the last band is truncated, only treat it as File when the row
    # before it isn't already unambiguously tall (i.e. already File itself).
    last = trailing[-1]
    if last[1] >= H - 1:
        preceding_is_tall = len(trailing) >= 2 and (trailing[-2][1] - trailing[-2][0]) > 35
        rows["file"] = trailing[-2] if preceding_is_tall else last
    else:
        rows["file"] = max(trailing, key=lambda band: band[1] - band[0])

    # Subject row: [white input][Post button]. Post button is the widget
    # span starting right after the input ends.
    y0, y1 = rows["subject"]
    y_mid = (y0 + y1) // 2
    input_span = _find_white_span(img, y_mid, content_x0, min(content_x1, W))
    if not input_span:
        raise RuntimeError("Couldn't locate the subject input field")
    button_span = _find_widget_span(img, y_mid, input_span[1], min(content_x1, W))
    if not button_span:
        raise RuntimeError("Couldn't locate the Post button in the subject row")
    post_x = (button_span[0] + button_span[1]) // 2
    post_y = y_mid

    y0, y1 = rows["comment"]
    y_mid = (y0 + y1) // 2
    span = _find_white_span(img, y_mid, content_x0, min(content_x1, W))
    if not span:
        raise RuntimeError("Couldn't locate the comment textarea")
    comment_x = (span[0] + span[1]) // 2
    comment_y = (y0 + y1) // 2

    # File row is a drag-and-drop zone on the live site, not a native
    # button -- just click its horizontal center to trigger the hidden input.
    y0, y1 = rows["file"]
    y_mid = (y0 + y1) // 2
    file_x = (content_x0 + min(content_x1, W)) // 2
    file_y = y_mid

    return {
        "resolution": f"{W}x{H}",
        "comment_field": [round(comment_x / W, 4), round(comment_y / H, 4)],
        "file_browse_button": [round(file_x / W, 4), round(file_y / H, 4)],
        "post_button": [round(post_x / W, 4), round(post_y / H, 4)],
    }


def write_profile_to_toml(profile):
    resolution = profile.pop("resolution")
    with open(TOML_PATH, "rb") as f:
        raw = f.read()

    header = f'[remote_browser.vnc_fallback.profiles."{resolution}"]\n'
    lines = "\n".join(f"{k} = {v}" for k, v in profile.items())
    block = f"\n{header}{lines}\n"

    text = raw.decode()
    marker = f'[remote_browser.vnc_fallback.profiles."{resolution}"]'
    if marker in text:
        pattern = re.compile(re.escape(marker) + r".*?(?=\n\[|\Z)", re.S)
        text = pattern.sub(block.strip("\n"), text)
    else:
        text = text.rstrip("\n") + "\n" + block

    with open(TOML_PATH, "w") as f:
        f.write(text)


def main():
    from soyduel import load_config, thread_url

    parser = argparse.ArgumentParser(description="Auto-calibrate VNC-fallback click regions")
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("--password", default=None)
    parser.add_argument("--write", action="store_true", help="save the profile into soyduel.toml")
    args = parser.parse_args()

    url = thread_url(load_config())
    png_path = capture_calibration_screenshot(args.host, args.port, url, args.password)
    profile = detect_regions(png_path)
    print(profile)

    if args.write:
        write_profile_to_toml(dict(profile))
        print(f"Wrote profile for resolution {profile['resolution']} to {TOML_PATH}")

    vnc_api.shutdown()


if __name__ == "__main__":
    main()
