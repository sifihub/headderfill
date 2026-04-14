from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import random
import time
from dataclasses import dataclass
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from webdriver_manager.chrome import ChromeDriverManager, ChromeType

# ===== OPTIONAL UC =====
try:
    import undetected_chromedriver as uc
    UC_AVAILABLE = True
except Exception:
    uc = None
    UC_AVAILABLE = False

# ===== DEFAULTS =====
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_FINGERPRINT = {
    "user_agent": DEFAULT_UA,
    "window_width": 1345,
    "window_height": 610,
    "timezone": "Asia/Kolkata",
    "language": "en-US",
    "languages": ["en-US", "en"],
    "platform": "Win32",
    "vendor": "Google Inc.",
    "hardware_concurrency": 8,
    "device_memory": 8,
    "device_scale_factor": 1,
}

# ===== CONFIG =====
STATIC_CHROME_ARGUMENTS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-gpu",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-site-isolation-trials",
]

@dataclass
class BrowserBootstrap:
    driver: webdriver.Chrome
    fingerprint: dict
    browser_version: str | None
    window_size: tuple[int, int]

# ===== HELPERS =====
def human_delay(a=0.5, b=2.0):
    time.sleep(random.uniform(a, b))

def randomize_fingerprint(fp: dict) -> dict:
    fp = dict(fp)
    fp["hardware_concurrency"] = random.choice([4, 6, 8, 12])
    fp["device_memory"] = random.choice([4, 8, 16])
    fp["window_width"] += random.randint(-40, 40)
    fp["window_height"] += random.randint(-40, 40)
    return fp

def load_or_create_fingerprint(data_dir: Path) -> dict:
    fp_path = Path(data_dir) / "fingerprint.json"
    if fp_path.exists():
        try:
            data = json.loads(fp_path.read_text())
            if isinstance(data, dict):
                return randomize_fingerprint(data)
        except:
            pass
    fp = randomize_fingerprint(DEFAULT_FINGERPRINT)
    fp_path.parent.mkdir(parents=True, exist_ok=True)
    fp_path.write_text(json.dumps(fp, indent=2))
    return fp

def resolve_browser_binary(preferred_binary: str = "") -> str:
    candidates = [
        os.environ.get("ZARA_CHROMIUM_BINARY", "").strip(),
        preferred_binary,
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return ""

def build_options(profile_dir: Path, fingerprint: dict, browser_version=None, headless=True, preferred_binary=""):
    options = Options()

    binary = resolve_browser_binary(preferred_binary)
    if binary:
        options.binary_location = binary

    for arg in STATIC_CHROME_ARGUMENTS:
        options.add_argument(arg)

    proxy = os.environ.get("ZARA_PROXY", "").strip()
    if proxy:
        options.add_argument(f"--proxy-server={proxy}")

    options.add_argument(f"--user-agent={fingerprint['user_agent']}")
    options.add_argument(f"--window-size={fingerprint['window_width']},{fingerprint['window_height']}")
    options.add_argument(f"--lang={fingerprint['language']}")
    options.add_argument(f"--user-data-dir={profile_dir}")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--hide-scrollbars")

    return options

def detect_browser_version(options: Options):
    try:
        binary = options.binary_location or "chromium"
        result = subprocess.run([binary, "--version"], capture_output=True, text=True)
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", result.stdout)
        if match:
            return match.group(1)
    except:
        pass
    return None

def apply_stealth(driver, fingerprint, browser_version=None):
    payload = json.dumps(fingerprint)

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"""
        const fp = {payload};

        Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
        Object.defineProperty(navigator, 'platform', {{get: () => fp.platform}});
        Object.defineProperty(navigator, 'language', {{get: () => fp.language}});
        Object.defineProperty(navigator, 'languages', {{get: () => fp.languages}});
        Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => fp.hardware_concurrency}});
        Object.defineProperty(navigator, 'deviceMemory', {{get: () => fp.device_memory}});

        window.chrome = {{
            runtime: {{}},
            loadTimes: function(){{}},
            csi: function(){{}}
        }};

        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return "Intel Inc.";
            if (param === 37446) return "Intel Iris OpenGL Engine";
            return getParameter.call(this, param);
        }};

        const originalQuery = navigator.permissions.query;
        navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({{ state: Notification.permission }})
                : originalQuery(parameters)
        );

        Object.defineProperty(navigator, 'plugins', {{
            get: () => [{{
                name: "Chrome PDF Plugin",
                filename: "internal-pdf-viewer",
                description: "Portable Document Format"
            }}]
        }});

        Object.defineProperty(navigator, 'mediaDevices', {{
            get: () => undefined
        }});
        """
    })

def bootstrap_driver(profile_dir: Path, data_dir: Path, headless=True, preferred_binary="", logger=None):
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = load_or_create_fingerprint(data_dir)
    options = build_options(profile_dir, fingerprint, headless=headless, preferred_binary=preferred_binary)
    browser_version = detect_browser_version(options)

    driver_path = ChromeDriverManager().install()
    service = Service(driver_path)

    if UC_AVAILABLE:
        driver = uc.Chrome(
            options=options,
            use_subprocess=True,
            driver_executable_path=driver_path,
            version_main=int(browser_version.split(".")[0]) if browser_version else None,
        )
    else:
        driver = webdriver.Chrome(service=service, options=options)

    driver.set_window_size(fingerprint["window_width"], fingerprint["window_height"])

    apply_stealth(driver, fingerprint, browser_version)

    return BrowserBootstrap(
        driver=driver,
        fingerprint=fingerprint,
        browser_version=browser_version,
        window_size=(fingerprint["window_width"], fingerprint["window_height"]),
    )
