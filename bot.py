import os
import time
import json
import hmac
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests
import websocket
from dotenv import load_dotenv

# ============================================================
# FINAL MULTI-KEY FALLBACK ENTRY PRICE BOT + DASHBOARD
# ============================================================

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PERSISTENT_DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)

BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
