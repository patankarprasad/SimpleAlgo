import time
import logging
import pyotp
from SmartApi import SmartConnect
from utility.context import Context
import requests
# from credentials import (BOT_TOKEN, CHAT_ID, API_KEY,USER_ID,PIN,TOTP_KEY,CONSUMER_KEY,SECRET_KEY, LOGIN_PASSWORD,MOBILE_NUMBER,MPIN)
from user.credentials import (BOT_TOKEN, CHAT_ID, API_KEY,USER_ID,TOTP_KEY,consumer_key,mobile_number, totpkey,ucc) #login_password,
from user.credentials import ( ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_KEY)
import requests
from neo_api_client import NeoAPI
import json
import sys
from kiteconnect import KiteConnect
from urllib.parse import urlparse, parse_qs

MAX_RETRIES = 10  # Number of retry attempts
RETRY_DELAY = 2  # Delay in seconds between retries
LOGIN_MAX_RETRIES = 50  # Number of retry attempts
LOGIN_RETRY_DELAY = 2  # Delay in seconds between retries

def angel_login(ctx, apiKey=API_KEY, userid=USER_ID, totpKey=TOTP_KEY, max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY):
    """
    Performs Angel One login and stores obj inside ctx
    """

    obj = SmartConnect(api_key=apiKey)

    for attempt in range(1, max_retries + 1):
        try:
            totp = pyotp.TOTP(totpKey).now()
            data = obj.generateSession(userid, ctx.angel_pin, totp)

            refreshToken = data.get('data', {}).get('refreshToken')
            if refreshToken:
                obj.getProfile(refreshToken)
                ctx.obj = obj
                logging.info(f"Angel login successful for {ctx.clientname}")
                return

            logging.warning(f"Login attempt {attempt} failed, retrying...")
            time.sleep(retry_delay)

        except Exception as e:
            logging.error(f"Login attempt {attempt} error: {e}")
            time.sleep(retry_delay)

    # raise RuntimeError("Angel login failed after max retries")
    logging.error("All login attempts failed.")

    # message = f"Login failed for {ctx.clientname}"
    # safe_execute(send_telegram_message, message)

    sys.exit(1)  # non-zero so systemd Restart=on-failure retries login failures

def zerodha_login( ctx, api_key=ZERODHA_API_KEY, api_secret=ZERODHA_API_SECRET, user_id=ZERODHA_USER_ID, password=ZERODHA_PASSWORD, totp_key=ZERODHA_TOTP_KEY, max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY ):

    for attempt in range(1, max_retries + 1):
        try:
            kiteobj = KiteConnect(api_key=api_key)
            session = requests.Session()
            login_res = session.post( "https://kite.zerodha.com/api/login", { "user_id": user_id, "password": password } ).json()
            request_id = login_res["data"]["request_id"]
            session.post( "https://kite.zerodha.com/api/twofa", { "user_id": user_id, "request_id": request_id, "twofa_value": pyotp.TOTP(totp_key).now() } )
            try:
                api_session = session.get( f"https://kite.trade/connect/login?api_key={api_key}" )
                parsed = urlparse(api_session.url)
            except Exception as e:
                reqUrl = e.request.url
                parsed = urlparse(reqUrl)
            query_params = parse_qs(parsed.query)
            request_token = query_params["request_token"][0]
            data = kiteobj.generate_session( request_token, api_secret=api_secret )
            access_token = data["access_token"]
            kiteobj.set_access_token(access_token)
            ctx.kiteobj = kiteobj
            ctx.kite_access_token = access_token
            logging.info( f"Zerodha login successful for {ctx.clientname}" )
            return
        except Exception as e:
            logging.error( f"Zerodha login attempt {attempt} failed: {e}", exc_info=True )
            if attempt < max_retries:
                time.sleep(retry_delay)
    logging.error("All Zerodha login attempts failed")
    # message = f"Zerodha login failed for {ctx.clientname}"
    # try:
    #     safe_execute(send_telegram_message, message)
    # except:
    #     pass
    sys.exit(1)  # non-zero so systemd Restart=on-failure retries login failures

def kotak_login(ctx, consumer_key=consumer_key,  mobile_number=mobile_number,  max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY): #password=login_password,

    """
    Performs Kotak Neo login and stores client inside ctx
    """
   
    for attempt in range(1, max_retries + 1):
        try:
            totp = pyotp.TOTP(totpkey.strip())
            otp = totp.now()
            client = NeoAPI(environment = 'prod', access_token=None, neo_fin_key= None, consumer_key = consumer_key)
            loginResponse = client.totp_login(mobile_number = mobile_number, ucc =ucc, totp= pyotp.TOTP(totpkey).now())
            session_response = client.totp_validate(mpin=ctx.kotak_mpin)
            if loginResponse is None:
                raise ValueError("Kotak login failed: Received None response")
            logging.info(f"Kotak login successful for {ctx.clientname}")
            ctx.client = client
            return client
            # break  # Exit loop if successful
        except Exception as e:
            logging.error(f"Login attempt {attempt + 1} failed: {e}")
            attempt += 1
            if attempt < LOGIN_MAX_RETRIES:
                logging.info(f"Retrying in { LOGIN_RETRY_DELAY} seconds...")
                time.sleep( LOGIN_RETRY_DELAY)
            else:
                logging.error("Max login attempts reached. Giving up.")
                # message = f"Kotak login failed for {ctx.clientname}"
                # safe_execute(send_telegram_message, message)
                sys.exit(1)  # non-zero so systemd Restart=on-failure retries login failures
    
    # raise RuntimeError("Kotak login failed after max retries")

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    params = {"chat_id": CHAT_ID, "text": message}
    requests.get(url, params=params)

def safe_execute(func, *args, max_retries=3, retry_delay=2, **kwargs):
    """Retries a function call in case of failure, supporting both positional and keyword arguments."""
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)  # Execute function with both args and kwargs
            return result
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed for {func.__name__}: {e}", exc_info=True)
            if attempt < max_retries - 1:
                time.sleep(retry_delay)  # Wait before retrying
            else:
                logging.info(f"Max retries reached. {func.__name__} execution failed.")
    return None  # Return None if all retries fail

