"""
Run this script manually on a machine with a browser to obtain a Kite access token,
then paste the token into your VPS using kite_login.manual_set_token().

Usage:
    python utils/token_helper.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kiteconnect import KiteConnect
import config

kite = KiteConnect(api_key=config.KITE_API_KEY)
print("Open this URL in your browser and complete login:")
print(kite.login_url())
print()

redirect_url = input("Paste the full redirect URL here: ").strip()
from urllib.parse import urlparse, parse_qs
params = parse_qs(urlparse(redirect_url).query)
request_token = params["request_token"][0]

session = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
access_token = session["access_token"]

print(f"\nAccess token: {access_token}")
print("\nTo use on VPS, run:")
print(f"  python -c \"from kite_login import manual_set_token; manual_set_token('{access_token}')\"")

# Optionally auto-save locally
save = input("\nSave token locally now? [y/N]: ").strip().lower()
if save == "y":
    from kite_login import manual_set_token
    manual_set_token(access_token)
    print("Saved.")
