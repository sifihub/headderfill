from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains


HEADDERFILL_VERSION = "sifihub-headderfill-live-2026-05-13"

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

# =========================================================
# ORIGINAL TEMPLATE STRUCTURE PRESERVED
# =========================================================

CHROME_OPTIONS_CLASS = uc.ChromeOptions
WEBDRIVER_FACTORY = uc.Chrome
ACTION_CHAINS_CLASS = ActionChains

STATIC_CHROME_ARGUMENTS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--password-store=basic",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-gpu",
    "--renderer-process-limit=1",
    "--max-old-space-size=512",
    "--js-flags=--max-old-space-size=512",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--window-position=0,0",
    "--no-first-run",
    "--no-default-browser-check",
]

EXTRA_CHROME_ARGUMENTS: list[str] = []
EXTRA_BINARY_CANDIDATES: list[str] = []

FORCE_WINDOW_SIZE_AFTER_START: tuple[int, int] | None = None


@dataclass
class BrowserBootstrap:
    driver: uc.Chrome
    fingerprint: dict
    browser_version: str | None
    window_size: tuple[int, int]


def new_actions(driver):
    return ACTION_CHAINS_CLASS(driver)


def parse_env_list(raw: str) -> list[str]:
    if not raw:
        return []

    items: list[str] = []

    for chunk in raw.replace("\n", ",").replace(";", ",").split(","):
        value = chunk.strip()

        if value:
            items.append(value)

    return items


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
            loaded = json.loads(
                fp_path.read_text(encoding="utf-8")
            )

            if isinstance(loaded, dict):
                data = dict(DEFAULT_FINGERPRINT)

                data.update(loaded)

                if (
                    not isinstance(data.get("languages"), list)
                    or not data["languages"]
                ):
                    data["languages"] = list(
                        DEFAULT_FINGERPRINT["languages"]
                    )

                return data

        except Exception:
            pass

    data = dict(DEFAULT_FINGERPRINT)

    fp_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fp_path.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )

    return data


def sync_user_agent(
    user_agent: str,
    browser_version: str | None,
) -> str:

    ua = (user_agent or DEFAULT_UA).strip() or DEFAULT_UA

    if not browser_version or "Chrome/" not in ua:
        return ua

    return re.sub(
        r"Chrome/\d+\.\d+\.\d+\.\d+",
        f"Chrome/{browser_version}",
        ua,
    )


def resolve_browser_binary(
    preferred_binary: str = "",
) -> str:

    candidates = [
        os.environ.get(
            "ZARA_CHROMIUM_BINARY",
            "",
        ).strip(),

        os.environ.get(
            "CHROMIUM_PATH",
            "",
        ).strip(),

        preferred_binary.strip(),
    ]

    candidates.extend(
        str(Path(candidate).expanduser())
        for candidate in EXTRA_BINARY_CANDIDATES
        if candidate
    )

    candidates.extend(
        parse_env_list(
            os.environ.get(
                "ZARA_EXTRA_BINARY_CANDIDATES",
                "",
            )
        )
    )

    if os.name != "nt":

        candidates.extend(
            [
                "/usr/bin/ungoogled-chromium",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/snap/bin/chromium",
            ]
        )

        for command in (
            "ungoogled-chromium",
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        ):
            resolved = shutil.which(command)

            if resolved:
                candidates.append(resolved)

    for candidate in candidates:

        if not candidate:
            continue

        resolved_path = str(
            Path(candidate).expanduser()
        )

        if Path(resolved_path).exists():
            return resolved_path

    return ""


def profile_directory_name() -> str:
    return (
        os.environ.get(
            "ZARA_PROFILE_DIRECTORY",
            "Default",
        ).strip()
        or "Default"
    )


def resolve_window_size(
    fingerprint: dict,
) -> tuple[int, int]:

    override = parse_window_size(
        os.environ.get(
            "ZARA_WINDOW_SIZE",
            "",
        )
    )

    if override:
        return override

    return (
        int(
            fingerprint.get(
                "window_width",
                DEFAULT_FINGERPRINT["window_width"],
            )
        ),
        int(
            fingerprint.get(
                "window_height",
                DEFAULT_FINGERPRINT["window_height"],
            )
        ),
    )


# =========================================================
# PROFILE LOCK SYSTEM
# =========================================================

def pid_is_alive(pid):
    try:
        os.kill(pid, 0)

    except ProcessLookupError:
        return False

    except PermissionError:
        return True

    return True


def singleton_lock_owner(profile_dir):
    lock_path = profile_dir / "SingletonLock"

    try:
        target = os.readlink(lock_path)

    except OSError:
        return None, None

    if "-" not in target:
        return None, None

    host, pid_text = target.rsplit("-", 1)

    try:
        return host, int(pid_text)

    except ValueError:
        return host, None


def profile_has_live_lock(profile_dir):
    host, pid = singleton_lock_owner(profile_dir)

    if pid is None:
        return False, None

    current_host = socket.gethostname()

    same_host = host in {
        current_host,
        "localhost",
    }

    return same_host and pid_is_alive(pid), pid


def profile_process_pids(profile_dir):
    profile_arg = f"--user-data-dir={profile_dir}"

    try:
        result = subprocess.run(
            ["pgrep", "-f", "--", profile_arg],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    except (
        OSError,
        subprocess.TimeoutExpired,
    ):
        return []

    pids = []

    current_pid = os.getpid()

    for line in result.stdout.splitlines():

        try:
            pid = int(line.strip())

        except ValueError:
            continue

        if pid != current_pid:
            pids.append(pid)

    return pids


def terminate_profile_processes(profile_dir):
    pids = profile_process_pids(profile_dir)

    if not pids:
        return

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)

        except ProcessLookupError:
            pass

    for _ in range(20):

        alive = [
            pid for pid in pids
            if pid_is_alive(pid)
        ]

        if not alive:
            return

        time.sleep(0.25)

    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)

        except ProcessLookupError:
            pass


def cleanup_profile_runtime_artifacts(
    profile_dir: Path,
    logger: logging.Logger | None = None,
    quiet: bool = False,
    fail_on_live: bool = True,
) -> None:

    live_lock, pid = profile_has_live_lock(profile_dir)

    if (
        live_lock
        and os.environ.get(
            "FORCE_PROFILE_UNLOCK",
            "",
        ).strip()
        != "1"
        and fail_on_live
    ):
        raise RuntimeError(
            f"Chrome profile already open by pid {pid}: {profile_dir}"
        )

    if (
        live_lock
        and os.environ.get(
            "FORCE_PROFILE_UNLOCK",
            "",
        ).strip()
        != "1"
    ):
        return

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

        if (
            name not in transient_names
            and not name.startswith("Singleton")
        ):
            continue

        try:

            if path.is_dir():
                shutil.rmtree(
                    path,
                    ignore_errors=True,
                )

            else:
                path.unlink(missing_ok=True)

        except Exception as exc:

            if logger:
                logger.warning(
                    "Could not remove stale profile artifact %s: %s",
                    path,
                    exc,
                )


def build_options(
    profile_dir: Path,
    fingerprint: dict,
    browser_version: str | None = None,
    *,
    headless: bool = True,
    preferred_binary: str = "",
):

    options = CHROME_OPTIONS_CLASS()

    browser_binary = resolve_browser_binary(
        preferred_binary
    )

    if browser_binary:
        options.binary_location = browser_binary

    width, height = resolve_window_size(
        fingerprint
    )

    for argument in STATIC_CHROME_ARGUMENTS:
        options.add_argument(argument)

    for argument in EXTRA_CHROME_ARGUMENTS:

        if argument:
            options.add_argument(argument)

    for argument in parse_env_list(
        os.environ.get(
            "ZARA_EXTRA_CHROME_ARGUMENTS",
            "",
        )
    ):
        options.add_argument(argument)

    options.add_argument(
        f"--user-agent={sync_user_agent(str(fingerprint.get('user_agent', DEFAULT_UA)), browser_version)}"
    )

    options.add_argument(
        f"--window-size={width},{height}"
    )

    options.add_argument(
        f"--lang={fingerprint.get('language', DEFAULT_FINGERPRINT['language'])}"
    )

    options.add_argument(
        f"--user-data-dir={Path(profile_dir).resolve()}"
    )

    options.add_argument(
        f"--profile-directory={profile_directory_name()}"
    )

    options.add_argument(
        f"--force-device-scale-factor={fingerprint.get('device_scale_factor', 1)}"
    )

    if headless:
        options.add_argument("--headless=new")

    return options


def detect_browser_version(
    options,
    preferred_binary: str = "",
) -> str | None:

    candidates = []

    if getattr(options, "binary_location", None):
        candidates.append(options.binary_location)

    browser_binary = resolve_browser_binary(
        preferred_binary
    )

    if browser_binary:
        candidates.append(browser_binary)

    seen: set[str] = set()

    for candidate in candidates:

        if not candidate or candidate in seen:
            continue

        seen.add(candidate)

        if not Path(candidate).exists():
            continue

        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )

        except Exception:
            continue

        raw = (
            result.stdout
            or result.stderr
            or ""
        ).strip()

        match = re.search(
            r"(\d+\.\d+\.\d+\.\d+)",
            raw,
        )

        if match:
            return match.group(1)

    return None


def detect_browser_major_version(
    browser_version: str | None,
) -> int | None:

    env_version = os.environ.get(
        "CHROME_VERSION_MAIN",
        "",
    ).strip()

    if env_version:
        try:
            return int(env_version)

        except Exception:
            pass

    if not browser_version:
        return None

    try:
        return int(
            browser_version.split(".", 1)[0]
        )

    except Exception:
        return None


def apply_hardcoded_fingerprint(
    driver,
    fingerprint: dict,
    browser_version: str | None = None,
):

    width, height = resolve_window_size(
        fingerprint
    )

    scale = int(
        fingerprint.get(
            "device_scale_factor",
            DEFAULT_FINGERPRINT[
                "device_scale_factor"
            ],
        )
    )

    language = str(
        fingerprint.get(
            "language",
            DEFAULT_FINGERPRINT["language"],
        )
    )

    languages = fingerprint.get(
        "languages",
        DEFAULT_FINGERPRINT["languages"],
    )

    if (
        not isinstance(languages, list)
        or not languages
    ):
        languages = list(
            DEFAULT_FINGERPRINT["languages"]
        )

    payload = {
        "user_agent": sync_user_agent(
            str(
                fingerprint.get(
                    "user_agent",
                    DEFAULT_FINGERPRINT[
                        "user_agent"
                    ],
                )
            ),
            browser_version,
        ),
        "language": language,
        "languages": languages,
        "platform": str(
            fingerprint.get(
                "platform",
                DEFAULT_FINGERPRINT[
                    "platform"
                ],
            )
        ),
        "vendor": str(
            fingerprint.get(
                "vendor",
                DEFAULT_FINGERPRINT[
                    "vendor"
                ],
            )
        ),
        "timezone": str(
            fingerprint.get(
                "timezone",
                DEFAULT_FINGERPRINT[
                    "timezone"
                ],
            )
        ),
        "hardware_concurrency": int(
            fingerprint.get(
                "hardware_concurrency",
                DEFAULT_FINGERPRINT[
                    "hardware_concurrency"
                ],
            )
        ),
        "device_memory": int(
            fingerprint.get(
                "device_memory",
                DEFAULT_FINGERPRINT[
                    "device_memory"
                ],
            )
        ),
        "window_width": width,
        "window_height": height,
        "device_scale_factor": scale,
    }

    try:
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": payload[
                    "user_agent"
                ],
                "acceptLanguage": payload[
                    "language"
                ],
                "platform": payload[
                    "platform"
                ],
            },
        )

    except Exception:
        pass

    try:
        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {
                "timezoneId": payload[
                    "timezone"
                ]
            },
        )

    except Exception:
        pass

    try:
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": payload[
                    "window_width"
                ],
                "height": payload[
                    "window_height"
                ],
                "deviceScaleFactor": payload[
                    "device_scale_factor"
                ],
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

                Object.defineProperty(navigator, 'webdriver', {{
                    get: () => undefined
                }});

                Object.defineProperty(navigator, 'platform', {{
                    get: () => __headderfillFp.platform
                }});

                Object.defineProperty(navigator, 'language', {{
                    get: () => __headderfillFp.language
                }});

                Object.defineProperty(navigator, 'languages', {{
                    get: () => __headderfillFp.languages
                }});

                Object.defineProperty(navigator, 'vendor', {{
                    get: () => __headderfillFp.vendor
                }});

                Object.defineProperty(navigator, 'hardwareConcurrency', {{
                    get: () => __headderfillFp.hardware_concurrency
                }});

                Object.defineProperty(navigator, 'deviceMemory', {{
                    get: () => __headderfillFp.device_memory
                }});

                Object.defineProperty(screen, 'width', {{
                    get: () => __headderfillFp.window_width
                }});

                Object.defineProperty(screen, 'height', {{
                    get: () => __headderfillFp.window_height
                }});

                Object.defineProperty(window, 'devicePixelRatio', {{
                    get: () => __headderfillFp.device_scale_factor
                }});

                const _resolvedOptions =
                    Intl.DateTimeFormat.prototype.resolvedOptions;

                Intl.DateTimeFormat.prototype.resolvedOptions =
                    function(...args) {{

                    const result =
                        _resolvedOptions.apply(this, args);

                    result.timeZone =
                        __headderfillFp.timezone;

                    return result;
                }};

                Object.defineProperty(navigator, 'plugins', {{
                    get: () => [1,2,3,4,5]
                }});

                window.chrome =
                    window.chrome || {{
                        runtime: {{}}
                    }};
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

    profile_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    if logger:
        logger.info(
            "Using headderfill version %s",
            HEADDERFILL_VERSION,
        )

    fingerprint = load_or_create_fingerprint(
        data_dir
    )

    cleanup_profile_runtime_artifacts(
        profile_dir,
        logger=logger,
    )

    options = build_options(
        profile_dir,
        fingerprint,
        None,
        headless=headless,
        preferred_binary=preferred_binary,
    )

    browser_version = detect_browser_version(
        options,
        preferred_binary=preferred_binary,
    )

    browser_major_version = (
        detect_browser_major_version(
            browser_version
        )
    )

    options = build_options(
        profile_dir,
        fingerprint,
        browser_version,
        headless=headless,
        preferred_binary=preferred_binary,
    )

    driver = WEBDRIVER_FACTORY(
        options=options,
        version_main=browser_major_version,
        use_subprocess=True,
    )

    window_size = (
        FORCE_WINDOW_SIZE_AFTER_START
        or resolve_window_size(
            fingerprint
        )
    )

    try:
        driver.set_window_size(
            *window_size
        )

    except Exception:
        pass

    apply_hardcoded_fingerprint(
        driver,
        fingerprint,
        browser_version=browser_version,
    )

    return BrowserBootstrap(
        driver=driver,
        fingerprint=fingerprint,
        browser_version=browser_version,
        window_size=window_size,
    )
