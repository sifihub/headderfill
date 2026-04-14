from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from webdriver_manager.chrome import ChromeDriverManager, ChromeType


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


@dataclass
class BrowserBootstrap:
    driver: webdriver.Chrome
    fingerprint: dict
    browser_version: str | None
    window_size: tuple[int, int]


def new_actions(driver: webdriver.Chrome) -> ActionChains:
    return ActionChains(driver)


def parse_window_size(raw: str) -> tuple[int, int] | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    match = re.match(r"^\s*(\d+)\s*[x,]\s*(\d+)\s*$", value)
    if not match:
        return None
    width = max(320, int(match.group(1)))
    height = max(320, int(match.group(2)))
    return width, height


def load_or_create_fingerprint(data_dir: Path) -> dict:
    fp_path = Path(data_dir) / "fingerprint.json"
    if fp_path.exists():
        try:
            loaded = json.loads(fp_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = dict(DEFAULT_FINGERPRINT)
                data.update(loaded)
                if not isinstance(data.get("languages"), list) or not data["languages"]:
                    data["languages"] = list(DEFAULT_FINGERPRINT["languages"])
                return data
        except Exception:
            pass
    data = dict(DEFAULT_FINGERPRINT)
    fp_path.parent.mkdir(parents=True, exist_ok=True)
    fp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def sync_user_agent(user_agent: str, browser_version: str | None) -> str:
    ua = (user_agent or DEFAULT_UA).strip() or DEFAULT_UA
    if not browser_version or "Chrome/" not in ua:
        return ua
    return re.sub(r"Chrome/\d+\.\d+\.\d+\.\d+", f"Chrome/{browser_version}", ua)


def resolve_browser_binary(preferred_binary: str = "") -> str:
    candidates = [
        os.environ.get("ZARA_CHROMIUM_BINARY", "").strip(),
        os.environ.get("CHROMIUM_PATH", "").strip(),
        (preferred_binary or "").strip(),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def profile_directory_name() -> str:
    return os.environ.get("ZARA_PROFILE_DIRECTORY", "Default").strip() or "Default"


def resolve_window_size(fingerprint: dict) -> tuple[int, int]:
    override = parse_window_size(os.environ.get("ZARA_WINDOW_SIZE", ""))
    if override:
        return override
    return (
        int(fingerprint.get("window_width", DEFAULT_FINGERPRINT["window_width"])),
        int(fingerprint.get("window_height", DEFAULT_FINGERPRINT["window_height"])),
    )


def build_options(
    profile_dir: Path,
    fingerprint: dict,
    browser_version: str | None = None,
    *,
    headless: bool = True,
    preferred_binary: str = "",
) -> Options:
    options = Options()
    browser_binary = resolve_browser_binary(preferred_binary)
    if browser_binary:
        options.binary_location = browser_binary
    width, height = resolve_window_size(fingerprint)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-gpu")
    options.add_argument("--renderer-process-limit=1")
    options.add_argument("--max-old-space-size=512")
    options.add_argument("--js-flags=--max-old-space-size=512")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument(f"--user-agent={sync_user_agent(str(fingerprint.get('user_agent', DEFAULT_UA)), browser_version)}")
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument(f"--lang={fingerprint.get('language', DEFAULT_FINGERPRINT['language'])}")
    options.add_argument(f"--user-data-dir={Path(profile_dir).resolve()}")
    options.add_argument(f"--profile-directory={profile_directory_name()}")
    options.add_argument("--password-store=basic")
    options.add_argument(f"--force-device-scale-factor={fingerprint.get('device_scale_factor', 1)}")
    options.add_argument("--window-position=0,0")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if headless:
        options.add_argument("--headless=new")
    return options


def detect_browser_version(options: Options, preferred_binary: str = "") -> str | None:
    candidates = []
    if options.binary_location:
        candidates.append(options.binary_location)
    browser_binary = resolve_browser_binary(preferred_binary)
    if browser_binary:
        candidates.append(browser_binary)
    if os.name != "nt":
        candidates.extend(
            [
                "/usr/bin/ungoogled-chromium",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/snap/bin/chromium",
            ]
        )

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if not Path(candidate).exists():
            continue
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        raw = (result.stdout or result.stderr or "").strip()
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", raw)
        if match:
            return match.group(1)
    return None


def clear_driver_cache_if_requested(logger: logging.Logger | None = None) -> None:
    if os.environ.get("ZARA_CLEAR_WDM_CACHE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    cache_dir = Path.home() / ".wdm" / "drivers"
    if not cache_dir.exists():
        return
    try:
        shutil.rmtree(cache_dir)
        if logger:
            logger.info("Cleared webdriver-manager cache at %s", cache_dir)
    except Exception as exc:
        if logger:
            logger.warning("Could not clear webdriver-manager cache at %s: %s", cache_dir, exc)


def cleanup_profile_runtime_artifacts(profile_dir: Path, logger: logging.Logger | None = None) -> None:
    transient_names = {
        "DevToolsActivePort",
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
        "lockfile",
        "LOCK",
    }
    for path in Path(profile_dir).rglob("*"):
        if not path.exists():
            continue
        name = path.name
        if name not in transient_names and not name.startswith("Singleton"):
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except Exception as exc:
            if logger:
                logger.warning("Could not remove stale profile artifact %s: %s", path, exc)


def resolve_driver_binary(installed_path: str) -> Path:
    candidate = Path(installed_path)
    exact_matches: list[Path] = []
    fallback_matches: list[Path] = []
    seen: set[str] = set()

    def looks_executable(path: Path) -> bool:
        try:
            head = path.read_bytes()[:4]
        except OSError:
            return False
        return head.startswith(b"\x7fELF") or head.startswith(b"MZ") or head.startswith(b"#!")

    def remember(path: Path, *, exact: bool) -> None:
        key = str(path)
        if key in seen or not path.exists() or not path.is_file():
            return
        seen.add(key)
        name = path.name.lower()
        if "chromedriver" not in name or "third_party_notices" in name or "license" in name:
            return
        if exact:
            exact_matches.append(path)
        else:
            fallback_matches.append(path)

    search_roots = [candidate.parent]
    if candidate.parent.exists():
        search_roots.extend(entry for entry in candidate.parent.iterdir() if entry.is_dir())

    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            if name in {"chromedriver", "chromedriver.exe"}:
                remember(path, exact=True)
            elif name.startswith("chromedriver"):
                remember(path, exact=False)

    for path in exact_matches + fallback_matches:
        if not looks_executable(path):
            continue
        if os.name != "nt":
            mode = path.stat().st_mode
            if not mode & 0o111:
                path.chmod(mode | 0o755)
        return path

    raise FileNotFoundError(f"Unable to locate a usable chromedriver binary near {installed_path}")


def apply_hardcoded_fingerprint(
    driver: webdriver.Chrome,
    fingerprint: dict,
    browser_version: str | None = None,
) -> None:
    width, height = resolve_window_size(fingerprint)
    scale = int(fingerprint.get("device_scale_factor", DEFAULT_FINGERPRINT["device_scale_factor"]))
    language = str(fingerprint.get("language", DEFAULT_FINGERPRINT["language"]))
    languages = fingerprint.get("languages", DEFAULT_FINGERPRINT["languages"])
    if not isinstance(languages, list) or not languages:
        languages = list(DEFAULT_FINGERPRINT["languages"])
    payload = {
        "user_agent": sync_user_agent(str(fingerprint.get("user_agent", DEFAULT_FINGERPRINT["user_agent"])), browser_version),
        "language": language,
        "languages": languages,
        "platform": str(fingerprint.get("platform", DEFAULT_FINGERPRINT["platform"])),
        "vendor": str(fingerprint.get("vendor", DEFAULT_FINGERPRINT["vendor"])),
        "timezone": str(fingerprint.get("timezone", DEFAULT_FINGERPRINT["timezone"])),
        "hardware_concurrency": int(fingerprint.get("hardware_concurrency", DEFAULT_FINGERPRINT["hardware_concurrency"])),
        "device_memory": int(fingerprint.get("device_memory", DEFAULT_FINGERPRINT["device_memory"])),
        "window_width": width,
        "window_height": height,
        "device_scale_factor": scale,
    }
    try:
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": payload["user_agent"],
                "acceptLanguage": payload["language"],
                "platform": payload["platform"],
            },
        )
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": payload["timezone"]})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": payload["window_width"],
                "height": payload["window_height"],
                "deviceScaleFactor": payload["device_scale_factor"],
                "mobile": False,
            },
        )
    except Exception:
        pass
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": f"""
                const __headderfillFp = {json.dumps(payload)};
                Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
                Object.defineProperty(navigator, 'platform', {{get: () => __headderfillFp.platform}});
                Object.defineProperty(navigator, 'language', {{get: () => __headderfillFp.language}});
                Object.defineProperty(navigator, 'languages', {{get: () => __headderfillFp.languages}});
                Object.defineProperty(navigator, 'vendor', {{get: () => __headderfillFp.vendor}});
                Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => __headderfillFp.hardware_concurrency}});
                Object.defineProperty(navigator, 'deviceMemory', {{get: () => __headderfillFp.device_memory}});
                Object.defineProperty(screen, 'width', {{get: () => __headderfillFp.window_width}});
                Object.defineProperty(screen, 'height', {{get: () => __headderfillFp.window_height}});
                Object.defineProperty(window, 'devicePixelRatio', {{get: () => __headderfillFp.device_scale_factor}});
                const _resolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
                Intl.DateTimeFormat.prototype.resolvedOptions = function(...args) {{
                    const result = _resolvedOptions.apply(this, args);
                    result.timeZone = __headderfillFp.timezone;
                    return result;
                }};
                Object.defineProperty(navigator, 'plugins', {{get: () => [1, 2, 3, 4, 5]}});
                window.chrome = window.chrome || {{runtime: {{}}}};
            """
        },
    )


def bootstrap_driver(
    profile_dir: Path,
    data_dir: Path,
    *,
    headless: bool = True,
    preferred_binary: str = "",
    logger: logging.Logger | None = None,
) -> BrowserBootstrap:
    profile_dir = Path(profile_dir).resolve()
    data_dir = Path(data_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = load_or_create_fingerprint(data_dir)
    cleanup_profile_runtime_artifacts(profile_dir, logger=logger)
    options = build_options(profile_dir, fingerprint, None, headless=headless, preferred_binary=preferred_binary)
    browser_version = detect_browser_version(options, preferred_binary=preferred_binary)
    clear_driver_cache_if_requested(logger=logger)
    options = build_options(
        profile_dir,
        fingerprint,
        browser_version,
        headless=headless,
        preferred_binary=preferred_binary,
    )
    manager_kwargs: dict[str, str | ChromeType] = {"chrome_type": ChromeType.CHROMIUM}
    if browser_version:
        manager_kwargs["driver_version"] = browser_version
        if logger:
            logger.info("Matching chromedriver to browser version %s", browser_version)
    try:
        installed_driver_path = ChromeDriverManager(**manager_kwargs).install()
    except Exception:
        if not browser_version:
            raise
        major = browser_version.split(".", 1)[0]
        if logger:
            logger.warning(
                "Exact chromedriver lookup failed for %s; retrying with major version %s",
                browser_version,
                major,
            )
        installed_driver_path = ChromeDriverManager(driver_version=major, chrome_type=ChromeType.CHROMIUM).install()
    driver_path = resolve_driver_binary(installed_driver_path)
    if logger and str(driver_path) != installed_driver_path:
        logger.warning("webdriver-manager returned %s; using %s instead", installed_driver_path, driver_path)
    service = Service(executable_path=str(driver_path))
    driver = webdriver.Chrome(service=service, options=options)
    window_size = resolve_window_size(fingerprint)
    try:
        driver.set_window_size(*window_size)
    except Exception:
        pass
    apply_hardcoded_fingerprint(driver, fingerprint, browser_version=browser_version)
    return BrowserBootstrap(
        driver=driver,
        fingerprint=fingerprint,
        browser_version=browser_version,
        window_size=window_size,
    )
