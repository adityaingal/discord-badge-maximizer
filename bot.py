from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Any

import nextcord
from nextcord import Interaction
from nextcord.ext import commands
from dotenv import load_dotenv
from web_server import start_web_server, start_keep_alive_pinger

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BOT_START_TIME = time.time()
WORKSPACE_DIR = Path(__file__).resolve().parent
STATE_STORAGE_PATH = WORKSPACE_DIR / "session.json"
DATABASE_STORAGE_PATH = (
    WORKSPACE_DIR / "data" / "games.json"
    if (WORKSPACE_DIR / "data").exists()
    else (WORKSPACE_DIR.parent / "data" / "games.json"
          if (WORKSPACE_DIR.parent / "data").exists()
          else WORKSPACE_DIR / "data" / "games.json")
)

intents = nextcord.Intents.default()
intents.message_content = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

REST_IDENTITY_URL = "https://discord.com/api/v9/users/@me?with_analytics_token=true"
DISCORD_USERS_API = "https://discord.com/api/v9/users"
TELEMETRY_INGEST_URL = "https://discord.com/api/v9/science"
UPSTREAM_METRICS_URL = "https://cordapi.dolfi.es/api/v2/properties/windows"
DISCORD_CATALOG_URL = "https://cdn.discordapp.com/detectables/games.json"

DISPATCH_BATCH_LIMIT = 50
NETWORK_COOLDOWN_SEC = 0.3
TOKEN_EXPIRY_THRESHOLD_SEC = 12 * 3600

DESKTOP_CLIENT_VERSION = "1.0.9253"
DESKTOP_BUILD_IDENTIFIER = 594031
DESKTOP_BUILD_NUMBER = DESKTOP_BUILD_IDENTIFIER
PLATFORM_BUILD_IDENTIFIER = 88414
PLATFORM_OS_REVISION = "10.0.26200"
USER_AGENT_IDENTIFIER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9253 Chrome/148.0.7778.280 "
    "Electron/42.7.1 Safari/537.36"
)


def extract_account_snowflake(auth_token: str) -> str:
    try:
        prefix = auth_token.split(".", 1)[0]
        padding = "=" * (-len(prefix) % 4)
        return base64.b64decode(prefix + padding).decode("utf-8")
    except Exception:
        return ""


def decode_token_snowflake(token: str) -> str:
    return extract_account_snowflake(token)


def generate_fallback_properties() -> dict:
    return {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": DESKTOP_CLIENT_VERSION,
        "os_version": PLATFORM_OS_REVISION,
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "has_client_mods": False,
        "browser_user_agent": USER_AGENT_IDENTIFIER,
        "browser_version": "42.7.1",
        "os_sdk_version": "26200",
        "client_build_number": DESKTOP_BUILD_IDENTIFIER,
        "native_build_number": PLATFORM_BUILD_IDENTIFIER,
        "client_event_source": None,
        "client_app_state": "focused",
    }


def assemble_super_properties(signature_id: str, session_id: str) -> str:
    try:
        network_req = urllib.request.Request(
            UPSTREAM_METRICS_URL,
            method="POST",
            data=b"{}",
            headers={"content-type": "application/json", "user-agent": USER_AGENT_IDENTIFIER}
        )
        with urllib.request.urlopen(network_req, timeout=10) as net_resp:
            properties_payload = json.loads(net_resp.read().decode("utf-8"))["properties"]
    except Exception:
        properties_payload = generate_fallback_properties()

    properties_payload["client_launch_id"] = str(uuid.uuid4())
    properties_payload["launch_signature"] = signature_id
    properties_payload["client_heartbeat_session_id"] = session_id
    serialized = json.dumps(properties_payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(serialized).decode("ascii")


def generate_fallback_super_props() -> str:
    props = generate_fallback_properties()
    props["client_launch_id"] = str(uuid.uuid4())
    props["launch_signature"] = str(uuid.uuid4())
    props["client_heartbeat_session_id"] = str(uuid.uuid4())
    serialized = json.dumps(props, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(serialized).decode("ascii")


def request_analytics_credential(auth_token: str, super_properties_b64: str) -> str:
    network_req = urllib.request.Request(
        REST_IDENTITY_URL,
        headers={
            "authorization": auth_token,
            "user-agent": USER_AGENT_IDENTIFIER,
            "x-super-properties": super_properties_b64,
        }
    )
    try:
        with urllib.request.urlopen(network_req, timeout=15) as net_resp:
            parsed = json.loads(net_resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Authentication rejected by endpoint (HTTP {exc.code})") from exc

    credential = parsed.get("analytics_token")
    if not credential:
        raise RuntimeError("Upstream response omitted analytics_token field.")
    return credential


def synchronize_credentials(state: dict, super_props_b64: str) -> None:
    try:
        analytics_token = request_analytics_credential(state["token"], super_props_b64)
        state["analytics_token"] = analytics_token
        state["fetched_at"] = int(time.time())
    except Exception as exc:
        raise RuntimeError(f"Failed to synchronize credentials: {exc}")


@dataclass
class AccountState:
    token: str = ""
    cookie: str = ""
    fingerprint: str = ""
    analytics_token: str = ""
    fetched_at: int = 0
    used_games: list[str] = field(default_factory=list)
    total_games_claimed: int = 0
    total_hours_claimed: float = 0.0
    video_path: str = ""
    total_stream_hours_claimed: float = 0.0
    total_stream_sessions: int = 0
    last_guild_id: str = ""
    last_channel_id: str = ""
    viewer_token: str = ""
    auto_stream: bool = False

    @property
    def requires_refresh(self) -> bool:
        return not self.analytics_token or (time.time() - self.fetched_at) > TOKEN_EXPIRY_THRESHOLD_SEC

    @property
    def has_credentials(self) -> bool:
        return bool(self.token.strip()) and bool(self.cookie.strip())


class MultiAccountManager:
    def __init__(self) -> None:
        self.accounts: Dict[str, AccountState] = {}
        self.load()

    def load(self) -> None:
        raw: dict | None = None

        # 1. Check if SESSION_JSON environment variable is set (Render secret or .env)
        env_session = os.getenv("SESSION_JSON", "").strip()
        # Strip surrounding quotes if added in Render UI
        if (env_session.startswith('"') and env_session.endswith('"')) or (env_session.startswith("'") and env_session.endswith("'")):
            env_session = env_session[1:-1].strip()

        if env_session and env_session not in ("{}", "null", "none"):
            # Attempt 1: Raw JSON format
            if env_session.startswith("{"):
                try:
                    parsed = json.loads(env_session)
                    if isinstance(parsed, dict):
                        raw = parsed
                except Exception:
                    pass

            # Attempt 2: Base64-encoded JSON format
            if raw is None:
                try:
                    clean_b64 = re.sub(r"[^A-Za-z0-9+/=]", "", env_session)
                    pad_len = (-len(clean_b64)) % 4
                    if pad_len:
                        clean_b64 += "=" * pad_len
                    decoded = base64.b64decode(clean_b64).decode("utf-8", errors="ignore")
                    parsed = json.loads(decoded)
                    if isinstance(parsed, dict):
                        raw = parsed
                except Exception:
                    pass

            if raw is not None:
                acct_count = len(raw.get("accounts", {})) or (1 if raw.get("token") else 0)
                print(f"[OK] Successfully loaded {acct_count} account(s) from SESSION_JSON environment variable.")
                try:
                    STATE_STORAGE_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")
                except Exception:
                    pass
            else:
                print("[!] Warning: Could not parse SESSION_JSON env var. Set valid base64 in Render or use /connect in Discord.")

        # 2. If not loaded from env, try reading from STATE_STORAGE_PATH
        if raw is None and STATE_STORAGE_PATH.exists():
            try:
                raw = json.loads(STATE_STORAGE_PATH.read_text(encoding="utf-8"))
            except Exception:
                raw = None

        if not raw:
            self.accounts = {}
            return

        # 3. Parse accounts dictionary
        accounts_data = raw.get("accounts", {})
        known_keys = set(AccountState().__dict__.keys())

        for snowflake, data in accounts_data.items():
            filtered = {k: v for k, v in data.items() if k in known_keys}
            # Fall back to root guild/channel if account's values are empty
            if not filtered.get("last_guild_id") and raw.get("last_guild_id"):
                filtered["last_guild_id"] = str(raw.get("last_guild_id"))
            if not filtered.get("last_channel_id") and raw.get("last_channel_id"):
                filtered["last_channel_id"] = str(raw.get("last_channel_id"))
            self.accounts[snowflake] = AccountState(**filtered)

        # 4. If accounts dict was empty, but top-level token exists (single account compatibility)
        root_token = raw.get("token", "").strip()
        if not self.accounts and root_token:
            snowflake = extract_account_snowflake(root_token)
            if snowflake:
                filtered = {k: v for k, v in raw.items() if k in known_keys}
                self.accounts[snowflake] = AccountState(**filtered)
                print(f"[OK] Initialized account {snowflake[:8]} from single-account session structure.")

    def save(self) -> None:
        existing = {}
        if STATE_STORAGE_PATH.exists():
            try:
                existing = json.loads(STATE_STORAGE_PATH.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        accounts_serialized = {}
        for snowflake, state in self.accounts.items():
            accounts_serialized[snowflake] = asdict(state)
        existing["accounts"] = accounts_serialized
        # Also sync first account to root for backward compatibility with games.py & stream.py
        if self.accounts:
            first_sf = next(iter(self.accounts))
            first_acct = self.accounts[first_sf]
            for key, val in asdict(first_acct).items():
                if key != "accounts":
                    existing[key] = val
        STATE_STORAGE_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def get_account(self, snowflake: str) -> AccountState:
        if snowflake not in self.accounts:
            self.accounts[snowflake] = AccountState()
        return self.accounts[snowflake]

    def persist(self) -> None:
        self.save()


multi_account_mgr = MultiAccountManager()

# Active stream tracking: snowflake -> thread info
active_streams: Dict[str, dict] = {}


def run_stream_engine(snowflake: str, guild_id: str, channel_id: str, duration_hours: float) -> None:
    """Run the GatewayStreamEngine in a background thread with auto-reconnect."""
    try:
        from stream import (
            State,
            Session,
            GatewayStreamEngine,
            Console,
            MiniWebSocket,
            ViewerEngine,
            GATEWAY_URL,
            _user_id_from_token,
        )

        acct = multi_account_mgr.get_account(snowflake)
        state = State(
            token=acct.token,
            cookie=acct.cookie,
            fingerprint=acct.fingerprint,
            analytics_token=acct.analytics_token,
            fetched_at=acct.fetched_at,
            total_stream_hours_claimed=acct.total_stream_hours_claimed,
            total_stream_sessions=acct.total_stream_sessions,
            last_guild_id=guild_id,
            last_channel_id=channel_id,
            viewer_token=acct.viewer_token,
            video_path=acct.video_path,
        )
        session = Session.new()
        console = Console()
        engine = GatewayStreamEngine(state, session, console)

        engine.guild_id = guild_id
        engine.channel_id = channel_id

        total_stream_seconds = 0.0
        end_time = time.time() + (duration_hours * 3600)
        reconnect_attempts = 0
        max_reconnects = 50

        while time.time() < end_time and snowflake in active_streams:
            C = console
            C.rule()
            C.info(f"connecting to discord gateway... (attempt {reconnect_attempts + 1})")

            engine.ws = MiniWebSocket(GATEWAY_URL, path="/?v=9&encoding=json")
            try:
                engine.ws.connect()
            except Exception as exc:
                C.err("gateway", f"connection failed: {exc}")
                reconnect_attempts += 1
                if reconnect_attempts >= max_reconnects:
                    C.err("stream", "max reconnection attempts reached")
                    break
                time.sleep(min(30, reconnect_attempts * 2))
                continue

            C.ok("gateway", "connected")
            engine.running = True
            engine.start_time = time.time()
            engine.counted_stream_seconds = 0.0
            engine.user_id = _user_id_from_token(state.token) or ""
            engine.stream_ever_active = False
            engine._stream_recreate_attempts = 0
            engine._user_left_vc = False
            engine._was_already_in_vc = False
            engine.ready_received.clear()

            event_thread = threading.Thread(target=engine._event_loop, daemon=True)
            event_thread.start()

            engine.ready_received.wait(timeout=5.0)

            if not engine.running:
                reconnect_attempts += 1
                if reconnect_attempts >= max_reconnects:
                    break
                time.sleep(min(30, reconnect_attempts * 2))
                continue

            engine.guild_id = guild_id
            engine.channel_id = channel_id

            # The alt viewer has to sit in the channel for Discord to count
            # the streaming hours, so it is started before the broadcast.
            if state.has_viewer_token:
                if engine.viewer_engine is None:
                    engine.viewer_engine = ViewerEngine(state.viewer_token, C)
                    engine.viewer_engine.start(guild_id, channel_id)
                    C.info("viewer", "automated alt viewer launched")
            else:
                C.warn("viewer", "no alt viewer token configured - Discord will not count the hours without a viewer")

            # The account may already sit in the channel (a previous session or
            # the real Discord client). Discord ignores a plain voice state
            # update when nothing changes, so no voice server answer would
            # arrive and the stream would never start. Wait for the answer
            # first and only leave and join again when it does not come, so
            # the account is not kicked out of the channel needlessly.
            if engine.detected_channel_id and str(engine.detected_channel_id) == str(channel_id):
                engine._was_already_in_vc = True
                C.info("voice", "account is already in this channel - waiting for the voice assignment")
                if not engine._join_voice_and_wait(8.0):
                    C.info("voice", "no voice server assignment - leaving and joining again")
                    engine._force_rejoin_voice()
            else:
                try:
                    engine.ws.send(json.dumps({
                        "op": 4,
                        "d": {
                            "guild_id": guild_id,
                            "channel_id": channel_id,
                            "self_mute": False,
                            "self_deaf": False,
                            "self_video": False,
                        },
                    }))
                    C.ok("voice", f"joining voice channel ({channel_id})")
                except Exception as exc:
                    C.err("voice", f"failed to join: {exc}")
                    reconnect_attempts += 1
                    if reconnect_attempts >= max_reconnects:
                        break
                    time.sleep(min(30, reconnect_attempts * 2))
                    continue

            if engine._ticker_thread is None or not engine._ticker_thread.is_alive():
                engine._ticker_thread = threading.Thread(target=engine._stream_ticker, daemon=True)
                engine._ticker_thread.start()

            if engine._watchdog_thread is None or not engine._watchdog_thread.is_alive():
                engine._watchdog_thread = threading.Thread(target=engine._watchdog_loop, daemon=True)
                engine._watchdog_thread.start()

            C.ok("stream", f"stream active for {duration_hours}h")
            C.info("use /stopstream to stop early")

            session_start = time.time()
            while engine.running and time.time() < end_time and snowflake in active_streams:
                time.sleep(1.0)

            session_duration = time.time() - session_start
            total_stream_seconds += engine.counted_stream_seconds

            if snowflake not in active_streams:
                C.info("stream", "stopped by user")
                break

            if time.time() >= end_time:
                C.ok("stream", "duration completed")
                break

            C.warn("stream", f"connection lost after {session_duration/3600:.1f}h, reconnecting...")
            reconnect_attempts += 1
            if reconnect_attempts >= max_reconnects:
                C.err("stream", "max reconnection attempts reached")
                break
            time.sleep(min(30, reconnect_attempts * 2))

        try:
            engine._cleanup(guild_id, interactive=False)
        except Exception:
            pass

        acct.total_stream_hours_claimed += total_stream_seconds / 3600.0
        acct.total_stream_sessions += 1
        multi_account_mgr.persist()
        active_streams.pop(snowflake, None)
        C.ok("stream", f"stream ended. Total: {total_stream_seconds/3600:.1f}h")

    except Exception as e:
        active_streams.pop(snowflake, None)
        print(f"Stream error for {snowflake}: {e}")


def load_game_catalog() -> list:
    cached_content = ""
    if DATABASE_STORAGE_PATH.exists():
        cached_content = DATABASE_STORAGE_PATH.read_text(encoding="utf-8").strip()
    if cached_content:
        try:
            titles = json.loads(cached_content)
            if titles:
                return titles
        except (json.JSONDecodeError, ValueError):
            pass
    try:
        req = urllib.request.Request(DISCORD_CATALOG_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
        DATABASE_STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATABASE_STORAGE_PATH.write_text(content, encoding="utf-8")
        return json.loads(content)
    except Exception:
        return []


def identify_windows_binary(game_record: dict) -> str:
    for target in game_record.get("executables", []):
        if target.get("os") == "win32" and target.get("name"):
            return target["name"]
    return "game.exe"


def parse_catalog_entries(raw_json: str) -> list:
    payload = json.loads(raw_json)
    catalog = []
    registered_ids = set()

    for item in payload:
        record_id = str(item.get("id", ""))
        if not record_id.isdigit() or record_id in registered_ids:
            continue
        if not any(entry.get("os") == "win32" and entry.get("name") for entry in item.get("executables", [])):
            continue
        registered_ids.add(record_id)
        catalog.append({
            "title_id": record_id,
            "display_name": item.get("name", "Unknown Game"),
            "binary_name": identify_windows_binary(item)
        })

    return catalog


# --- UI Components ---

class ConnectModal(nextcord.ui.Modal):
    def __init__(self):
        super().__init__(title="Discord Account Connection")
        self.add_item(nextcord.ui.TextInput(
            label="Discord Authorization Token",
            placeholder="Paste your Discord user token...",
            required=True,
            max_length=2000
        ))
        self.add_item(nextcord.ui.TextInput(
            label="cf_clearance Cookie",
            placeholder="Paste your cf_clearance cookie...",
            required=True,
            max_length=2000
        ))

    async def callback(self, interaction: Interaction):
        token = self.children[0].value.strip()
        cookie = self.children[1].value.strip()

        snowflake = decode_token_snowflake(token)
        if not snowflake:
            await interaction.response.send_message("Invalid Discord token format.", ephemeral=True)
            return

        state = multi_account_mgr.get_account(snowflake)
        state.token = token
        state.cookie = cookie
        multi_account_mgr.persist()

        await interaction.response.send_message(
            f"Account connected! Snowflake: {snowflake[:8]}...{snowflake[-4:]}\n"
            f"Use /accounts to verify or /game to start claiming badges.",
            ephemeral=True
        )


class AccountSelectView(nextcord.ui.View):
    def __init__(self, accounts: list[str], timeout: float = 60.0):
        super().__init__(timeout=timeout)
        options = [
            nextcord.SelectOption(label=f"Account {i+1}", value=snowflake)
            for i, snowflake in enumerate(accounts)
        ]
        self.select_menu = nextcord.ui.Select(
            placeholder="Select account to use",
            options=options
        )
        self.add_item(self.select_menu)
        self.select_menu.callback = self._on_select

    async def _on_select(self, interaction: Interaction):
        await interaction.response.defer()
        selected = self.select_menu.values[0]
        interaction.client.selected_account = selected
        await interaction.followup.send(
            f"Selected account: {selected[:8]}...{selected[-4:]}",
            ephemeral=True
        )


class GameClaimModal(nextcord.ui.Modal):
    def __init__(self, snowflake: str):
        super().__init__(title="Game Badge Claimer")
        self.snowflake = snowflake
        self.add_item(nextcord.ui.TextInput(
            label="Target Games Count",
            placeholder="e.g. 100 or 'all'",
            required=True,
            max_length=50
        ))
        self.add_item(nextcord.ui.TextInput(
            label="Hours per Game",
            placeholder="e.g. 50",
            required=True,
            max_length=20
        ))

    async def callback(self, interaction: Interaction):
        raw_count = self.children[0].value.strip()
        raw_hours = self.children[1].value.strip()

        try:
            target_count = int(raw_count) if raw_count.lower() != "all" else "all"
        except ValueError:
            await interaction.response.send_message("Invalid game count specified.", ephemeral=True)
            return

        try:
            target_hours = float(raw_hours) if raw_hours else 1.0
        except ValueError:
            await interaction.response.send_message("Invalid hours specified.", ephemeral=True)
            return

        state = multi_account_mgr.get_account(self.snowflake)
        if not state.has_credentials:
            await interaction.response.send_message(
                "No credentials for this account. Use /connect first.",
                ephemeral=True
            )
            return

        catalog_raw = load_game_catalog()
        if isinstance(catalog_raw, str):
            catalog = parse_catalog_entries(catalog_raw)
        else:
            catalog = catalog_raw

        if not catalog:
            await interaction.response.send_message("Unable to load game catalog.", ephemeral=True)
            return

        existing_pool = {item["title_id"] for item in catalog}
        unprocessed = [item["title_id"] for item in catalog if item["title_id"] not in state.used_games]
        processed = [gid for gid in state.used_games if gid in existing_pool]

        if isinstance(target_count, int) and len(unprocessed) >= target_count:
            chosen = unprocessed[:target_count]
        else:
            chosen = unprocessed if target_count == "all" else []
            if processed and isinstance(target_count, int):
                needed = target_count - len(chosen)
                rotated = processed[needed:] + processed[:needed]
                chosen.extend(rotated[:needed])

        state.used_games = [gid for gid in state.used_games if gid not in chosen] + chosen
        registry = {item["title_id"]: item for item in catalog}
        selected_titles = [registry[gid] for gid in chosen if gid in registry]

        if not selected_titles:
            await interaction.response.send_message("No titles selected.", ephemeral=True)
            return

        duration_ms = int(target_hours * 3600 * 1000)

        try:
            super_props = generate_fallback_super_props()
            synchronize_credentials(state, super_props)
        except Exception as exc:
            await interaction.response.send_message(f"Auth failed: {exc}", ephemeral=True)
            return

        from games import TerminalInterface, IngestionPipeline, RuntimeClientContext, TelemetryEventFabricator

        terminal = TerminalInterface()
        runtime_context = RuntimeClientContext.initialize()
        fabricator = TelemetryEventFabricator(state, runtime_context)

        valid_titles = []
        for title in selected_titles:
            title["binary_name"] = title.get("binary_name", "game.exe")
            valid_titles.append(title)

        engine = IngestionPipeline(fabricator, terminal)
        accepted = engine.execute_stream(
            valid_titles,
            lambda title: fabricator.assemble_complete_session(title, duration_ms)
        )

        state.total_games_claimed += accepted
        state.total_hours_claimed += accepted * target_hours
        multi_account_mgr.persist()

        await interaction.response.send_message(
            f"Processed {accepted}/{len(valid_titles)} titles ({accepted * target_hours:,.0f}h total)\n"
            f"Badge updates in 24-48h.",
            ephemeral=True
        )


class StreamClaimModal(nextcord.ui.Modal):
    def __init__(self, snowflake: str):
        super().__init__(title="Stream Badge Claimer")
        self.snowflake = snowflake
        self.add_item(nextcord.ui.TextInput(
            label="Duration (hours)",
            placeholder="e.g. 5",
            required=True,
            max_length=10
        ))
        self.add_item(nextcord.ui.TextInput(
            label="Server (Guild) ID",
            placeholder="e.g. 123456789",
            required=True,
            max_length=20
        ))
        self.add_item(nextcord.ui.TextInput(
            label="Voice Channel ID",
            placeholder="e.g. 987654321",
            required=True,
            max_length=20
        ))

    async def callback(self, interaction: Interaction):
        try:
            duration = float(self.children[0].value.strip())
        except ValueError:
            await interaction.response.send_message("Invalid duration specified.", ephemeral=True)
            return

        guild_id = self.children[1].value.strip()
        channel_id = self.children[2].value.strip()

        state = multi_account_mgr.get_account(self.snowflake)
        if not state.has_credentials:
            await interaction.response.send_message(
                "No credentials for this account. Use /connect first.",
                ephemeral=True
            )
            return

        video_path = os.path.join(WORKSPACE_DIR, "assets", "video.mp4")
        if not os.path.exists(video_path):
            video_path = os.path.join(WORKSPACE_DIR, "assets", "video2.mp4")

        state.video_path = video_path
        state.last_guild_id = guild_id
        state.last_channel_id = channel_id
        state.auto_stream = True
        multi_account_mgr.persist()

        if not os.path.exists(video_path):
            await interaction.response.send_message(
                "Video file not found. Ensure assets/video.mp4 exists.",
                ephemeral=True
            )
            return

        if self.snowflake in active_streams:
            await interaction.response.send_message(
                "This account is already streaming. Use /stopstream first.",
                ephemeral=True
            )
            return

        thread = threading.Thread(
            target=run_stream_engine,
            args=(self.snowflake, guild_id, channel_id, duration),
            daemon=True
        )
        active_streams[self.snowflake] = {
            "thread": thread,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "started_at": time.time(),
            "duration": duration,
        }
        thread.start()

        await interaction.response.send_message(
            f"Stream started!\n"
            f"- Duration: {duration} hours\n"
            f"- Guild: {guild_id}\n"
            f"- Channel: {channel_id}\n"
            f"- Video: {os.path.basename(video_path)}\n\n"
            f"Use /stopstream to stop early.\n"
            f"Requirements: alt account viewer in voice channel, personalization setting ON.",
            ephemeral=True
        )

class Stream24Modal(nextcord.ui.Modal):
    def __init__(self, snowflake: str):
        super().__init__(title="24/7 Stream Setup")
        self.snowflake = snowflake
        self.add_item(nextcord.ui.TextInput(
            label="Server (Guild) ID",
            placeholder="e.g. 123456789",
            required=True,
            max_length=20
        ))
        self.add_item(nextcord.ui.TextInput(
            label="Voice Channel ID",
            placeholder="e.g. 987654321",
            required=True,
            max_length=20
        ))

    async def callback(self, interaction: Interaction):
        guild_id = self.children[0].value.strip()
        channel_id = self.children[1].value.strip()

        state = multi_account_mgr.get_account(self.snowflake)
        if not state.has_credentials:
            await interaction.response.send_message(
                "No credentials for this account. Use /connect first.",
                ephemeral=True
            )
            return

        video_path = os.path.join(WORKSPACE_DIR, "assets", "video.mp4")
        if not os.path.exists(video_path):
            video_path = os.path.join(WORKSPACE_DIR, "assets", "video2.mp4")

        state.video_path = video_path
        state.last_guild_id = guild_id
        state.last_channel_id = channel_id
        state.auto_stream = True
        multi_account_mgr.persist()

        if not os.path.exists(video_path):
            await interaction.response.send_message(
                "Video file not found. Ensure assets/video.mp4 exists.",
                ephemeral=True
            )
            return

        if self.snowflake in active_streams:
            await interaction.response.send_message(
                "This account is already streaming. Use /stopstream first.",
                ephemeral=True
            )
            return

        duration = 24.0
        thread = threading.Thread(
            target=run_stream_engine,
            args=(self.snowflake, guild_id, channel_id, duration),
            daemon=True
        )
        active_streams[self.snowflake] = {
            "thread": thread,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "started_at": time.time(),
            "duration": duration,
        }
        thread.start()

        await interaction.response.send_message(
            f"24/7 Stream started!\n"
            f"- Duration: 24 hours\n"
            f"- Guild: {guild_id}\n"
            f"- Channel: {channel_id}\n"
            f"- Video: {os.path.basename(video_path)}\n\n"
            f"Use /streamstatus to check status.\n"
            f"Use /stopstream to stop early.",
            ephemeral=True
        )


class AccountModalSelectView(nextcord.ui.View):
    """Select an account and open the matching modal for it."""

    def __init__(self, accounts: list[str], modal_factory, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.modal_factory = modal_factory
        options = [
            nextcord.SelectOption(label=f"Account {i+1}", value=snowflake)
            for i, snowflake in enumerate(accounts)
        ]
        self.select_menu = nextcord.ui.Select(
            placeholder="Select account to use",
            options=options
        )
        self.add_item(self.select_menu)
        self.select_menu.callback = self._on_select

    async def _on_select(self, interaction: Interaction):
        await interaction.response.send_modal(self.modal_factory(self.select_menu.values[0]))


class ViewerTokenModal(nextcord.ui.Modal):
    def __init__(self, snowflake: str):
        super().__init__(title="Alt Viewer Account")
        self.snowflake = snowflake
        self.add_item(nextcord.ui.TextInput(
            label="Alt Account Token",
            placeholder="Paste the alt account token (leave empty to clear)",
            required=False,
            max_length=2000
        ))

    async def callback(self, interaction: Interaction):
        token = self.children[0].value.strip()
        state = multi_account_mgr.get_account(self.snowflake)
        state.viewer_token = token
        multi_account_mgr.persist()
        if token:
            await interaction.response.send_message(
                "Alt viewer token saved. It joins the channel and watches the stream\n"
                "automatically - Discord needs a watcher for the hours to count.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Alt viewer token cleared.",
                ephemeral=True
            )


# --- Slash Commands ---

@bot.slash_command(name="connect", description="Connect a Discord account")
async def connect_command(interaction: Interaction):
    await interaction.response.send_modal(ConnectModal())


@bot.slash_command(name="setviewer", description="Set the alt account that watches the stream")
async def setviewer_command(interaction: Interaction):
    accounts = list(multi_account_mgr.accounts.keys())

    if not accounts:
        await interaction.response.send_message(
            "No accounts connected. Use /connect first.",
            ephemeral=True
        )
        return

    if len(accounts) == 1:
        await interaction.response.send_modal(ViewerTokenModal(accounts[0]))
    else:
        view = AccountModalSelectView(accounts, ViewerTokenModal)
        await interaction.response.send_message(
            "Select an account to use:",
            view=view,
            ephemeral=True
        )


@bot.slash_command(name="accounts", description="List connected accounts")
async def accounts_command(interaction: Interaction):
    accounts = list(multi_account_mgr.accounts.keys())

    if not accounts:
        await interaction.response.send_message(
            "No accounts connected. Use /connect to add one.",
            ephemeral=True
        )
        return

    lines = [f"Connected Accounts ({len(accounts)} total)"]
    for i, snowflake in enumerate(accounts):
        state = multi_account_mgr.get_account(snowflake)
        lines.append(
            f"{i+1}. `{snowflake[:8]}...{snowflake[-4:]}` | "
            f"Creds: {'Yes' if state.has_credentials else 'No'} | "
            f"Alt viewer: {'Yes' if state.viewer_token else 'No'} | "
            f"Games: {state.total_games_claimed} | "
            f"Hours: {state.total_hours_claimed:,.0f}h"
        )

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.slash_command(name="game", description="Claim Game Time & Game Variety badges")
async def game_command(interaction: Interaction):
    accounts = list(multi_account_mgr.accounts.keys())

    if not accounts:
        await interaction.response.send_message(
            "No accounts connected. Use /connect first.",
            ephemeral=True
        )
        return

    if len(accounts) == 1:
        await interaction.response.send_modal(GameClaimModal(accounts[0]))
    else:
        view = AccountSelectView(accounts)
        await interaction.response.send_message(
            "Select an account to use:",
            view=view,
            ephemeral=True
        )


@bot.slash_command(name="stream", description="Start streaming for Streaming Badge")
async def stream_command(interaction: Interaction):
    accounts = list(multi_account_mgr.accounts.keys())

    if not accounts:
        await interaction.response.send_message(
            "No accounts connected. Use /connect first.",
            ephemeral=True
        )
        return

    if len(accounts) == 1:
        await interaction.response.send_modal(StreamClaimModal(accounts[0]))
    else:
        view = AccountSelectView(accounts)
        await interaction.response.send_message(
            "Select an account to use:",
            view=view,
            ephemeral=True
        )


@bot.slash_command(name="stream24", description="Start a 24-hour stream")
async def stream24_command(interaction: Interaction):
    accounts = list(multi_account_mgr.accounts.keys())

    if not accounts:
        await interaction.response.send_message(
            "No accounts connected. Use /connect first.",
            ephemeral=True
        )
        return

    if len(accounts) == 1:
        await interaction.response.send_modal(Stream24Modal(accounts[0]))
    else:
        view = AccountSelectView(accounts)
        await interaction.response.send_message(
            "Select an account to use:",
            view=view,
            ephemeral=True
        )


@bot.slash_command(name="streamstatus", description="Check active stream status")
async def streamstatus_command(interaction: Interaction):
    if not active_streams:
        await interaction.response.send_message(
            "No active streams.",
            ephemeral=True
        )
        return

    lines = [f"Active Streams ({len(active_streams)})"]
    for snowflake, info in active_streams.items():
        elapsed = time.time() - info["started_at"]
        remaining = max(0, info["duration"] * 3600 - elapsed)
        lines.append(
            f"- `{snowflake[:8]}...{snowflake[-4:]}` | "
            f"Elapsed: {elapsed/3600:.1f}h | "
            f"Remaining: {remaining/3600:.1f}h | "
            f"Channel: {info['channel_id']}"
        )

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


VIDEOS_DIR = WORKSPACE_DIR / "videos"


@bot.slash_command(name="uploadvideo", description="Upload a video file for streaming")
async def uploadvideo_command(interaction: Interaction, file: nextcord.Attachment):
    if not file.filename.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
        await interaction.response.send_message(
            "Invalid file type. Please upload a video file (mp4, mkv, avi, mov, webm).",
            ephemeral=True
        )
        return

    VIDEOS_DIR.mkdir(exist_ok=True)
    save_path = VIDEOS_DIR / file.filename

    try:
        await file.save(save_path)
        await interaction.response.send_message(
            f"Video uploaded: {file.filename}\nUse /listvideos to see all videos.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"Failed to save video: {e}",
            ephemeral=True
        )


@bot.slash_command(name="listvideos", description="List available videos for streaming")
async def listvideos_command(interaction: Interaction):
    VIDEOS_DIR.mkdir(exist_ok=True)
    videos = [f.name for f in VIDEOS_DIR.iterdir() if f.suffix.lower() in ('.mp4', '.mkv', '.avi', '.mov', '.webm')]

    if not videos:
        await interaction.response.send_message(
            "No videos found. Use /uploadvideo to add one.",
            ephemeral=True
        )
        return

    lines = [f"Available Videos ({len(videos)})"]
    for i, v in enumerate(videos, 1):
        lines.append(f"{i}. {v}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.slash_command(name="removevideo", description="Remove a video file")
async def removevideo_command(interaction: Interaction):
    VIDEOS_DIR.mkdir(exist_ok=True)
    videos = [f for f in VIDEOS_DIR.iterdir() if f.suffix.lower() in ('.mp4', '.mkv', '.avi', '.mov', '.webm')]

    if not videos:
        await interaction.response.send_message(
            "No videos to remove.",
            ephemeral=True
        )
        return

    options = [
        nextcord.SelectOption(label=f.name[:80], value=f.name)
        for f in videos[:25]
    ]
    select = nextcord.ui.Select(placeholder="Select video to remove", options=options)
    view = nextcord.ui.View(timeout=60.0)
    view.add_item(select)

    async def _on_select(inter: Interaction):
        filename = select.values[0]
        try:
            (VIDEOS_DIR / filename).unlink()
            await inter.response.send_message(f"Removed: {filename}", ephemeral=True)
        except Exception as e:
            await inter.response.send_message(f"Failed to remove: {e}", ephemeral=True)

    select.callback = _on_select
    await interaction.response.send_message("Select a video to remove:", view=view, ephemeral=True)


@bot.slash_command(name="streamvideo", description="Stream a selected video")
async def streamvideo_command(interaction: Interaction):
    accounts = list(multi_account_mgr.accounts.keys())

    if not accounts:
        await interaction.response.send_message(
            "No accounts connected. Use /connect first.",
            ephemeral=True
        )
        return

    VIDEOS_DIR.mkdir(exist_ok=True)
    videos = [f for f in VIDEOS_DIR.iterdir() if f.suffix.lower() in ('.mp4', '.mkv', '.avi', '.mov', '.webm')]

    if not videos:
        await interaction.response.send_message(
            "No videos found. Use /uploadvideo to add one.",
            ephemeral=True
        )
        return

    options = [
        nextcord.SelectOption(label=f.name[:80], value=f.name)
        for f in videos[:25]
    ]
    select = nextcord.ui.Select(placeholder="Select video to stream", options=options)
    view = nextcord.ui.View(timeout=60.0)
    view.add_item(select)

    async def _on_select(inter: Interaction):
        # The option carries the file name only: Discord limits a select
        # option value to 100 characters and an absolute path easily
        # exceeds that, which makes the whole interaction fail with
        # "The application did not respond".
        video_path = str(VIDEOS_DIR / select.values[0])
        if len(accounts) == 1:
            await inter.response.send_modal(StreamVideoModal(accounts[0], video_path))
        else:
            account_view = AccountSelectView(accounts)
            await inter.response.send_message("Select account to use:", view=account_view, ephemeral=True)

    select.callback = _on_select
    await interaction.response.send_message("Select a video to stream:", view=view, ephemeral=True)


class StreamVideoModal(nextcord.ui.Modal):
    def __init__(self, snowflake: str, video_path: str):
        super().__init__(title="Stream Video")
        self.snowflake = snowflake
        self.video_path = video_path
        self.add_item(nextcord.ui.TextInput(
            label="Duration (hours)",
            placeholder="e.g. 24",
            required=True,
            max_length=10
        ))
        self.add_item(nextcord.ui.TextInput(
            label="Server (Guild) ID",
            placeholder="e.g. 123456789",
            required=True,
            max_length=20
        ))
        self.add_item(nextcord.ui.TextInput(
            label="Voice Channel ID",
            placeholder="e.g. 987654321",
            required=True,
            max_length=20
        ))

    async def callback(self, interaction: Interaction):
        try:
            duration = float(self.children[0].value.strip())
        except ValueError:
            await interaction.response.send_message("Invalid duration specified.", ephemeral=True)
            return

        guild_id = self.children[1].value.strip()
        channel_id = self.children[2].value.strip()

        state = multi_account_mgr.get_account(self.snowflake)
        if not state.has_credentials:
            await interaction.response.send_message(
                "No credentials for this account. Use /connect first.",
                ephemeral=True
            )
            return

        if not os.path.exists(self.video_path):
            await interaction.response.send_message(
                "Video file not found. Use /listvideos to see available videos.",
                ephemeral=True
            )
            return

        state.video_path = self.video_path
        state.last_guild_id = guild_id
        state.last_channel_id = channel_id
        state.auto_stream = True
        multi_account_mgr.persist()

        if self.snowflake in active_streams:
            await interaction.response.send_message(
                "This account is already streaming. Use /stopstream first.",
                ephemeral=True
            )
            return

        thread = threading.Thread(
            target=run_stream_engine,
            args=(self.snowflake, guild_id, channel_id, duration),
            daemon=True
        )
        active_streams[self.snowflake] = {
            "thread": thread,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "started_at": time.time(),
            "duration": duration,
        }
        thread.start()

        await interaction.response.send_message(
            f"Stream started!\n"
            f"- Video: {os.path.basename(self.video_path)}\n"
            f"- Duration: {duration} hours\n"
            f"- Guild: {guild_id}\n"
            f"- Channel: {channel_id}\n\n"
            f"Use /streamstatus to check status.\n"
            f"Use /stopstream to stop early.",
            ephemeral=True
        )


@bot.slash_command(name="stopstream", description="Stop an active stream")
async def stopstream_command(interaction: Interaction):
    accounts = list(multi_account_mgr.accounts.keys())

    if not accounts:
        await interaction.response.send_message(
            "No accounts connected.",
            ephemeral=True
        )
        return

    # Find which account is streaming
    streaming = [s for s in accounts if s in active_streams]

    if not streaming:
        await interaction.response.send_message(
            "No active streams found.",
            ephemeral=True
        )
        return

    if len(streaming) == 1:
        snowflake = streaming[0]
    else:
        view = AccountSelectView(streaming)
        await interaction.response.send_message(
            "Select which stream to stop:",
            view=view,
            ephemeral=True
        )
        return

    info = active_streams.pop(snowflake, None)
    if info:
        state = multi_account_mgr.get_account(snowflake)
        state.auto_stream = False
        multi_account_mgr.persist()
        await interaction.response.send_message(
            f"Stream stopped for {snowflake[:8]}...{snowflake[-4:]}\n"
            f"Duration was set to {info['duration']}h.",
            ephemeral=True
        )


@bot.slash_command(name="exportsession", description="Export session.json as base64 for Render SESSION_JSON env var")
async def exportsession_command(interaction: Interaction):
    if not STATE_STORAGE_PATH.exists():
        await interaction.response.send_message("No session.json found on disk.", ephemeral=True)
        return
    try:
        content = STATE_STORAGE_PATH.read_bytes()
        encoded = base64.b64encode(content).decode("ascii")
        await interaction.response.send_message(
            f"**Your SESSION_JSON for Render:**\n```\n{encoded}\n```\n"
            f"Copy and paste the above string into your Render Dashboard -> Environment -> `SESSION_JSON`.\n\n"
            f":rotating_light: **This string is a credential** - it contains your Discord "
            f"token and cookie. Paste it only into the Render environment field. "
            f"Pasting it into a browser address bar makes Chrome show a "
            f"\"Dangerous site\" warning and puts your token in the browser history. "
            f"Rotate the token afterwards if it was exposed anywhere.",
            ephemeral=True
        )
    except Exception as exc:
        await interaction.response.send_message(f"Failed to export session: {exc}", ephemeral=True)


@bot.slash_command(name="status", description="Show bot health, Render status, and active streams")
async def status_command(interaction: Interaction):
    port = os.getenv("PORT", "10000")
    render_url = os.getenv("RENDER_EXTERNAL_URL", "Local / Unset")
    uptime_sec = int(time.time() - BOT_START_TIME)
    uptime_str = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m {uptime_sec % 60}s"

    stream_lines = []
    for sf, info in active_streams.items():
        elapsed = (time.time() - info["started_at"]) / 3600.0
        stream_lines.append(f"• `{sf[:8]}...`: Guild `{info['guild_id']}`, Channel `{info['channel_id']}` ({elapsed:.1f}h / {info['duration']}h)")
    streams_text = "\n".join(stream_lines) if stream_lines else "None active"

    msg = (
        f"**🤖 Discord Badge Maximizer Status**\n"
        f"• **Render Web Port:** `{port}` (Healthy 200 OK)\n"
        f"• **Render URL:** `{render_url}`\n"
        f"• **Uptime:** `{uptime_str}`\n"
        f"• **Connected Accounts:** `{len(multi_account_mgr.accounts)}`\n"
        f"• **Active Streams ({len(active_streams)}):**\n{streams_text}\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)


def get_bot_status() -> dict:
    bot_ready = bot.is_ready()
    bot_user_str = str(bot.user) if bot_ready and bot.user else "Connecting..."
    streams_list = []
    for sf, info in list(active_streams.items()):
        elapsed = (time.time() - info.get("started_at", time.time())) / 3600.0
        streams_list.append({
            "snowflake": sf,
            "guild_id": info.get("guild_id"),
            "channel_id": info.get("channel_id"),
            "elapsed_hours": round(elapsed, 2),
            "duration_hours": info.get("duration", 0),
        })
    return {
        "status": "healthy",
        "bot_online": bot_ready,
        "bot_user": bot_user_str,
        "accounts_count": len(multi_account_mgr.accounts),
        "active_streams": streams_list,
        "uptime_seconds": int(time.time() - BOT_START_TIME),
    }


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} - {bot.user.id}")
    print("Multi-account badge maximizer bot ready")
    print(f"Connected accounts: {len(multi_account_mgr.accounts)}")

    # Check for auto-resume streams on startup (especially helpful after Render restarts)
    auto_stream_env = os.getenv("AUTO_STREAM", "true").lower() in ("true", "1", "yes")
    if auto_stream_env:
        for snowflake, acct in multi_account_mgr.accounts.items():
            if acct.has_credentials and acct.last_guild_id and acct.last_channel_id:
                if snowflake not in active_streams:
                    print(f"[OK] Auto-resuming 24h stream for {snowflake[:8]} in guild {acct.last_guild_id}, channel {acct.last_channel_id}...")
                    thread = threading.Thread(
                        target=run_stream_engine,
                        args=(snowflake, acct.last_guild_id, acct.last_channel_id, 24.0),
                        daemon=True
                    )
                    active_streams[snowflake] = {
                        "thread": thread,
                        "guild_id": acct.last_guild_id,
                        "channel_id": acct.last_channel_id,
                        "started_at": time.time(),
                        "duration": 24.0,
                    }
                    thread.start()


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not found.")
        print("Please configure DISCORD_BOT_TOKEN in Render Dashboard -> Environment Variables (or .env file).")
        sys.exit(1)

    # Immediately start the health check web server and keep-alive pinger for Render
    start_web_server(get_bot_status)
    start_keep_alive_pinger()

    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)