#!/usr/bin/env python3
"""
Interactive wizard to configure the [remote_browser] section of soyduel.toml:
VNC host/port, WebDriver vs VNC-fallback mode, password, and optional
auto-calibration + immediate start.
"""
import os
import re
import subprocess
import sys

from soyduel_calibrate import (
    capture_calibration_screenshot,
    detect_regions,
    write_profile_to_toml,
)
from vncdotool import api as vnc_api

HERE = os.path.dirname(os.path.abspath(__file__))
TOML_PATH = os.path.join(HERE, "soyduel.toml")


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def ask_yes_no(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def update_remote_browser_settings(enabled, webdriver_url, host, port, password):
    with open(TOML_PATH) as f:
        text = f.read()

    def set_key(text, key, value):
        pattern = re.compile(rf'^{re.escape(key)} = .*$', re.M)
        replacement = f'{key} = {value}'
        if pattern.search(text):
            return pattern.sub(replacement, text, count=1)
        raise RuntimeError(f"Couldn't find existing '{key}' key in {TOML_PATH} to update")

    text = set_key(text, "enabled", str(enabled).lower())
    text = set_key(text, "webdriver_url", f'"{webdriver_url}"')
    text = set_key(text, "vnc_host", f'"{host}"')
    text = set_key(text, "vnc_port", str(port))
    text = set_key(text, "vnc_password", f'"{password}"')

    with open(TOML_PATH, "w") as f:
        f.write(text)


def main():
    print("=== soyduel VNC bootstrap wizard ===\n")

    vnc_target = ask("Which VNC? (host:port)")
    if ":" not in vnc_target:
        raise SystemExit("Expected host:port, e.g. 58.20.117.40:5999")
    host, port_str = vnc_target.rsplit(":", 1)
    port = int(port_str)

    has_dom = ask_yes_no("Does it have DOM support? (geckodriver already listening remotely)", default=False)
    webdriver_url = ""
    if has_dom:
        webdriver_url = ask("geckodriver WebDriver URL", default=f"http://{host}:4444")

    use_auto_calibration = ask_yes_no(
        "Auto-calibrate VNC-fallback click regions now?",
        default=not has_dom,
    )

    password = ask("Password (blank for none)", default="") or ""

    print("\nWriting config to soyduel.toml ...")
    update_remote_browser_settings(
        enabled=True,
        webdriver_url=webdriver_url,
        host=host,
        port=port,
        password=password,
    )
    print("Done.")

    if use_auto_calibration:
        print("\nRunning auto-calibration ...")
        from soyduel import load_config, thread_url
        load_config.cache_clear()
        url = thread_url(load_config())
        png_path = capture_calibration_screenshot(host, port, url, password or None)
        profile = detect_regions(png_path)
        print("Detected profile:", profile)
        write_profile_to_toml(dict(profile))
        print(f"Wrote profile for resolution {profile['resolution']} to {TOML_PATH}")
        vnc_api.shutdown()

    print("\nAll set.")

    run_now = ask_yes_no("Would you like to run now with this config?", default=False)
    if run_now:
        print("\nStarting the bot ...")
        subprocess.run([sys.executable, os.path.join(HERE, "soyduelctl.py"), "start"], check=False)
    else:
        print("Review soyduel.toml, then start the bot with: python3 soyduelctl.py start")


if __name__ == "__main__":
    main()
