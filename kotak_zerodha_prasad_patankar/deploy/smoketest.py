#!/usr/bin/env python3
"""Post-install check: verify every dependency resolves and every project
module imports, WITHOUT starting the algo.

Deliberately does not import `main` -- that would perform a live Kotak and
Zerodha login and begin the trading loop.

Also parses user_selection.csv and reports which strategies are enabled, so a
folder that still carries another user's lot sizes is obvious before you start
the service.

    /opt/iob-venv/bin/python deploy/smoketest.py
"""

import importlib
import os
import pkgutil
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

failures = 0


def check(label, fn):
    global failures
    try:
        detail = fn()
        print(f"  ok    {label}" + (f" -- {detail}" if detail else ""))
    except Exception as exc:
        failures += 1
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")


print(f"app dir: {APP_DIR}\n")

# import name -> distribution name on PyPI (they differ for several of these)
DEPS = {
    "pandas": "pandas",
    "numpy": "numpy",
    "flask": "flask",
    "requests": "requests",
    "pyotp": "pyotp",
    "dateutil": "python-dateutil",
    "kiteconnect": "kiteconnect",
    "neo_api_client": "neo_api_client",
    "SmartApi": "smartapi-python",
    # Undeclared dep of smartapi-python -- checked by name so the failure says
    # "logzero" rather than surfacing as a confusing SmartApi import error.
    "logzero": "logzero",
}


def _dep(import_name, dist_name):
    importlib.import_module(import_name)
    try:
        from importlib.metadata import version
        return version(dist_name)
    except Exception:
        return ""


print("third-party dependencies:")
for _imp, _dist in DEPS.items():
    check(_imp, lambda i=_imp, d=_dist: _dep(i, d))

print("\nbroker SDK method surface:")


def _sdk_methods(import_path, cls_name, required):
    """Importing a broker SDK is not enough -- the old Kotak v1 package imports
    fine but has no totp_login/totp_validate, so login fails at 09:10 instead
    of at install time. Check the methods this codebase actually calls."""
    mod = importlib.import_module(import_path)
    cls = getattr(mod, cls_name)
    missing = [m for m in required if not hasattr(cls, m)]
    if missing:
        raise AttributeError(f"{cls_name} is missing {', '.join(missing)}")
    return f"{len(required)} methods present"


# Exactly what utility/common_functions.py and utility/orders.py invoke.
check("neo_api_client.NeoAPI", lambda: _sdk_methods(
    "neo_api_client", "NeoAPI",
    ["totp_login", "totp_validate", "place_order", "order_report",
     "positions", "cancel_order"]))

check("kiteconnect.KiteConnect", lambda: _sdk_methods(
    "kiteconnect", "KiteConnect",
    ["generate_session", "set_access_token", "ltp", "historical_data"]))

print("\nproject modules:")
for pkg in ("utility", "data", "strategy"):
    found = list(pkgutil.iter_modules([os.path.join(APP_DIR, pkg)]))
    if not found:
        failures += 1
        print(f"  FAIL  {pkg}/: no modules found")
        continue
    for mod in found:
        check(f"{pkg}.{mod.name}", lambda p=pkg, m=mod.name:
              (importlib.import_module(f"{p}.{m}"), "")[1])

print("\nconfig:")


def _config():
    from utility.config import load_user_selection
    cfg = load_user_selection(APP_DIR)
    on = sorted(k for k, v in cfg.items()
                if k.startswith("STRATEGY_") and v is True)
    print(f"  ok    user_selection.csv parsed -- {len(cfg)} keys")
    print(f"        strategies ON: {', '.join(on) if on else 'none'}")

    # int(v) because config.py casts '1' -> True and '0' -> False
    lots = {k: int(v) for k, v in cfg.items() if k.endswith("_LOT")}
    sizes = sorted(set(lots.values()))
    print(f"        lot multipliers: {len(lots)} keys, value(s) {sizes}")
    if sizes != [1]:
        print("        >> NOT all 1 -- confirm these match YOUR capital, not "
              "the folder this was cloned from")
    return cfg


def _login_ids(cfg):
    from utility.config import apply_login_config

    class _Ctx:
        pass

    try:
        apply_login_config(_Ctx(), cfg)
        print("  ok    SUBSCRIPTION_IDK / APPROVAL_IDA are populated")
    except ValueError as exc:
        # Expected until step 5 is done; not a failure, but must be loud.
        print(f"  TODO  login IDs not set yet -- {exc}")
        print("        the service cannot start until user_selection.csv has "
              "SUBSCRIPTION_IDK and APPROVAL_IDA")


try:
    cfg = _config()
    _login_ids(cfg)
except Exception as exc:
    failures += 1
    print(f"  FAIL  config: {type(exc).__name__}: {exc}")

print()
if failures:
    print(f"{failures} check(s) FAILED")
    sys.exit(1)
print("all checks passed")
