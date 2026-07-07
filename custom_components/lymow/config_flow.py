"""Config flow for Lymow — supports email/password AND Google OAuth."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.http import HomeAssistantView
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CognitoAuth, LymowAuthError, LymowClient
from .const import (
    AUTH_METHOD_GOOGLE,
    AUTH_METHOD_PASSWORD,
    CONF_AUTH_METHOD,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DOMAIN,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)

OAUTH_CALLBACK_PATH = "/api/lymow/oauth/callback"


class LymowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Lymow integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._auth:     CognitoAuth | None = None
        self._devices:  list[dict]         = []
        self._email     = ""
        self._password  = ""
        self._region    = "eu-west-1"
        self._oauth_state    = ""
        self._pkce_verifier  = ""
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # ── Step 1: Choose auth method ──────────────────────────────

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        if user_input:
            method = user_input.get(CONF_AUTH_METHOD, AUTH_METHOD_PASSWORD)
            self._region = user_input[CONF_REGION]
            if method == AUTH_METHOD_GOOGLE:
                return await self.async_step_google()
            return await self.async_step_password()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_REGION, default="us-east-2"): vol.In(REGIONS),
                vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_PASSWORD): vol.In({
                    AUTH_METHOD_PASSWORD: "Email & Password",
                    AUTH_METHOD_GOOGLE:   "Google Account (OAuth)",
                }),
            }),
        )

    # ── Step 2a: Email/password login (original flow) ───────────

    async def async_step_password(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input:
            email    = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            try:
                session = async_get_clientsession(self.hass)
                auth    = CognitoAuth(self._region, session)
                await auth.login(email, password)
                await auth.get_aws_credentials()

                client  = LymowClient(self._region, auth, session)
                devices = await client.get_device_list()

                self._auth     = auth
                self._devices  = devices
                self._email    = email
                self._password = password

            except LymowAuthError:
                errors["base"] = "invalid_auth"
            except aiohttp.ClientConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Lymow auth")
                errors["base"] = "unknown"
            else:
                if self._reauth_entry is not None:
                    return await self._finish_reauth()
                if not self._devices:
                    return self.async_abort(reason="no_devices")
                if len(self._devices) == 1:
                    return await self._create_entry(self._devices[0])
                return await self.async_step_select_device()

        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL):    str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    # ── Step 2b: Google OAuth flow ──────────────────────────────

    async def async_step_google(self, user_input: dict | None = None) -> FlowResult:
        """Show the user a link to Google OAuth login."""
        from homeassistant.components.http import HomeAssistantView
        self.hass.http.register_view(LymowOAuthStartView)
        
        if user_input is not None:
            code = user_input.get("code", "").strip()
            import re
            # Auto-extract code and state from pasted callback URLs
            m = re.search(r'[?&]code=([a-f0-9-]+)', code, re.I)
            if m:
                code = m.group(1)
            if not code:
                return self.async_show_form(
                    step_id="google",
                    data_schema=vol.Schema({vol.Required("code"): str}),
                    errors={"base": "invalid_auth"},
                    description_placeholders={"auth_url": self._oauth_url()},
                )

            # Validate state from pasted URL if present
            raw = user_input.get("code", "")
            state_match = re.search(r'[?&]state=([A-Za-z0-9_-]+)', raw)
            if state_match and self._oauth_state:
                if state_match.group(1) != self._oauth_state:
                    _LOGGER.warning("OAuth state mismatch — possible CSRF")
                    return self.async_show_form(
                        step_id="google",
                        data_schema=vol.Schema({vol.Required("code"): str}),
                        errors={"base": "invalid_auth"},
                        description_placeholders={"auth_url": self._oauth_url()},
                    )

            try:
                session = async_get_clientsession(self.hass)
                auth = CognitoAuth(self._region, session)
                redirect_uri = self._oauth_redirect_uri()
                await auth.exchange_oauth_code(
                    code, redirect_uri, code_verifier=self._pkce_verifier or None,
                )
                await auth.get_aws_credentials()

                client = LymowClient(self._region, auth, session)
                devices = await client.get_device_list()

                self._auth    = auth
                self._devices = devices
                self._email   = ""
                self._password = ""

            except LymowAuthError as e:
                _LOGGER.error("Google OAuth failed: %s", e)
                return self.async_show_form(
                    step_id="google",
                    data_schema=vol.Schema({vol.Required("code"): str}),
                    errors={"base": "invalid_auth"},
                    description_placeholders={"auth_url": self._oauth_url()},
                )
            except Exception:
                _LOGGER.exception("Unexpected error during Google OAuth")
                return self.async_show_form(
                    step_id="google",
                    data_schema=vol.Schema({vol.Required("code"): str}),
                    errors={"base": "unknown"},
                    description_placeholders={"auth_url": self._oauth_url()},
                )
            else:
                if self._reauth_entry is not None:
                    return await self._finish_reauth()
                if not self._devices:
                    return self.async_abort(reason="no_devices")
                if len(self._devices) == 1:
                    return await self._create_entry(self._devices[0])
                return await self.async_step_select_device()

        return self.async_show_form(
            step_id="google",
            data_schema=vol.Schema({vol.Required("code"): str}),
            description_placeholders={"auth_url": self._oauth_url()},
        )

    def _oauth_url(self) -> str:
        self._oauth_state = secrets.token_urlsafe(32)
        self._pkce_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self._pkce_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        base = self.hass.config.internal_url or "http://homeassistant.local:8123"
        import urllib.parse
        qs = urllib.parse.urlencode({
            "region": self._region,
            "state": self._oauth_state,
            "cc": code_challenge,
        })
        return f"{base}/api/lymow/oauth/start?{qs}"

    def _oauth_redirect_uri(self) -> str:
        # Must use the app's registered redirect URI with trailing slash
        return "myapp://callback/"

    # ── Step 3: Select device ───────────────────────────────────

    async def async_step_select_device(self, user_input: dict | None = None) -> FlowResult:
        if user_input:
            thing = user_input["thing_name"]
            device = next((d for d in self._devices if _thing_name(d) == thing), self._devices[0])
            return await self._create_entry(device)

        choices = {_thing_name(d): _label(d) for d in self._devices}
        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema({vol.Required("thing_name"): vol.In(choices)}),
        )

    async def _create_entry(self, device: dict) -> FlowResult:
        thing = _thing_name(device)
        await self.async_set_unique_id(f"{DOMAIN}_{thing}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=_label(device),
            data={
                CONF_EMAIL:      self._email,
                CONF_PASSWORD:   self._password,
                CONF_REGION:     self._region,
                CONF_AUTH_METHOD: AUTH_METHOD_GOOGLE if not self._password else AUTH_METHOD_PASSWORD,
                "thing_name":    thing,
                "device_name":   _label(device),
                "refresh_token": self._auth.refresh_token,
                "id_token":      self._auth.id_token,
            },
        )

    # ── Re-authentication ───────────────────────────────────────
    # Two entry points, both end in _finish_reauth (update tokens in place, no
    # delete/re-add): async_step_reauth = AUTOMATIC (HA raises this on
    # ConfigEntryAuthFailed — token expired); async_step_reconfigure = MANUAL
    # ("Reconfigure" button) so a user can refresh proactively any time.

    async def async_step_reauth(self, entry_data: dict) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._region = entry_data.get(CONF_REGION) or self._region
        self._email = entry_data.get(CONF_EMAIL, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(self, user_input: dict | None = None) -> FlowResult:
        """Manual 'Reconfigure' — re-authenticate on demand."""
        if self._reauth_entry is None:
            self._reauth_entry = self.hass.config_entries.async_get_entry(
                self.context["entry_id"]
            )
            self._region = self._reauth_entry.data.get(CONF_REGION, self._region)
            self._email = self._reauth_entry.data.get(CONF_EMAIL, "")
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> FlowResult:
        """Route to the auth method the entry was created with."""
        method = (
            self._reauth_entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_PASSWORD)
            if self._reauth_entry else AUTH_METHOD_PASSWORD
        )
        if method == AUTH_METHOD_GOOGLE:
            return await self.async_step_google(user_input)
        return await self.async_step_password(user_input)

    async def _finish_reauth(self) -> FlowResult:
        """Write fresh tokens onto the existing entry, reload it, and — for
        multi-mower accounts — propagate the same fresh tokens to sibling entries
        of the SAME Cognito account (matched by the id_token 'sub'), so one
        re-auth fixes every mower instead of N separate prompts."""
        new_tokens = {
            "refresh_token": self._auth.refresh_token,
            "id_token":      self._auth.id_token,
        }
        new_sub = _jwt_sub(self._auth.id_token)
        if new_sub:
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if entry.entry_id == self._reauth_entry.entry_id:
                    continue
                if _jwt_sub(entry.data.get("id_token", "")) == new_sub:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, **new_tokens}
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )

        new_data = {**self._reauth_entry.data, **new_tokens}
        if self._password:
            new_data[CONF_EMAIL] = self._email
            new_data[CONF_PASSWORD] = self._password
        return self.async_update_reload_and_abort(
            self._reauth_entry, data=new_data, reason="reauth_successful"
        )

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> LymowOptionsFlow:
        return LymowOptionsFlow(entry)


class LymowOptionsFlow(config_entries.OptionsFlowWithReload):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        from .const import DEFAULT_SCAN_INTERVAL
        if user_input:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    "scan_interval",
                    default=self._entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL),
                ): vol.All(int, vol.Range(min=10, max=300)),
                vol.Required(
                    "render_multiprocessing",
                    default=self._entry.options.get("render_multiprocessing", True),
                ): bool,
            }),
        )


class LymowOAuthStartView(HomeAssistantView):
    """Intercept the myapp:// redirect and display the code."""

    url = "/api/lymow/oauth/start"
    name = "api:lymow:oauth:start"
    requires_auth = False

    async def get(self, request):
        from aiohttp import web
        import html as html_mod
        region = request.query.get("region", "us-east-2")
        if region not in REGIONS:
            return web.Response(text="Invalid region", status=400)
        state = request.query.get("state", "")
        code_challenge = request.query.get("cc", "")
        hass = request.app["hass"]
        session = async_get_clientsession(hass)
        auth = CognitoAuth(region, session)
        auth_url = auth.get_oauth_authorize_url(
            redirect_uri="myapp://callback/",
            provider="Google",
            state=state or None,
            code_challenge=code_challenge or None,
        )
        safe_url = html_mod.escape(auth_url)
        page = f"""<!DOCTYPE html><html><head><title>Lymow - Sign in with Google</title>
<style>
body{{font-family:-apple-system,sans-serif;display:flex;justify-content:center;
     align-items:center;min-height:100vh;margin:0;background:#f0f2f5}}
.card{{background:#fff;border-radius:16px;padding:40px;max-width:580px;width:90%;
      box-shadow:0 4px 24px rgba(0,0,0,.12);text-align:center}}
h2{{color:#1a73e8;margin-bottom:8px}}
.sub{{color:#666;margin-bottom:24px}}
.btn{{background:#1a73e8;color:#fff;border:none;padding:14px 28px;border-radius:8px;
     font-size:16px;cursor:pointer;display:inline-block}}
.btn:hover{{background:#1557b0}}
.btn2{{background:#fff;color:#1a73e8;border:2px solid #1a73e8;padding:12px 24px;border-radius:8px;
      font-size:15px;cursor:pointer;display:inline-block}}
.btn2:hover{{background:#e8f0fe}}
.code-box{{font-size:15px;padding:14px;width:100%;border:2px solid #1a73e8;border-radius:8px;
          margin:12px 0;font-family:monospace;text-align:center;box-sizing:border-box}}
.ok{{color:#0d652d;background:#e6f4ea;padding:16px;border-radius:8px;margin-top:16px}}
.step{{background:#f8f9fa;border-radius:8px;padding:16px;margin:12px 0;text-align:left;line-height:2}}
.step b{{color:#1a73e8}}
kbd{{background:#e8e8e8;border:1px solid #b4b4b4;border-radius:3px;padding:2px 6px;font-size:13px}}
</style></head>
<body><div class="card">
<h2>Lymow + Google</h2>
<p class="sub">Connect your Lymow account via Google sign-in</p>
<input type="hidden" id="au" value="{safe_url}">

<div class="step">
  <b>1.</b> If not signed into Google yet, click <b>Sign in</b> first (opens new tab).<br>
  &nbsp;&nbsp;&nbsp;&nbsp;The tab may keep loading after sign-in &mdash; that is normal. Just close it.<br>
  <b>2.</b> Press <kbd>F12</kbd> &rarr; <b>Network</b> tab &rarr; check <b>Preserve log</b><br>
  <b>3.</b> Click <b>Get Code</b> (navigates this tab &mdash; keep DevTools open!)<br>
  <b>4.</b> Find <code>callback</code> in Network tab &rarr; right-click &rarr; <b>Copy URL</b><br>
  <b>5.</b> Go back to <b>Home Assistant</b> and paste it in the code field
</div>
<div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:16px 0">
  <button class="btn2" onclick="signIn()">Sign in with Google &nearr;</button>
  <button class="btn" onclick="getCode()">Get Code</button>
</div>
<p style="font-size:13px;color:#888">
  Already signed in? Skip straight to <b>Get Code</b>.
</p>

<hr style="margin:24px 0;border:none;border-top:1px solid #e0e0e0">
<p style="color:#666;font-size:14px"><b>Already have the code?</b> Paste below to extract &amp; copy:</p>
<input type="text" id="code" class="code-box"
       placeholder="Paste code or myapp://callback URL..." oninput="extractCode(this)">
<button class="btn" onclick="copyCode()">Copy Code</button>
<div id="ok" class="ok" style="display:none">
  Copied! Paste in <b>Home Assistant</b> and submit quickly &mdash; expires in ~60s.
</div>
</div>
<script>
var AU=document.getElementById("au").value;
function $(id){{return document.getElementById(id)}}
function signIn(){{window.open(AU,"_blank")}}
function getCode(){{window.location.href=AU}}
function extractCode(el){{
  var v=el.value.trim(),m=v.match(/[?&]code=([a-f0-9-]+)/i);
  if(m) el.value=m[1]
}}
function copyCode(){{
  var el=$("code"),c=el.value.trim();if(!c) return;
  var m=c.match(/[?&]code=([a-f0-9-]+)/i);
  if(m){{c=m[1];el.value=c}}
  try{{navigator.clipboard.writeText(c)}}catch(e){{}}
  el.select();try{{document.execCommand("copy")}}catch(e){{}}
  $("ok").style.display="block"
}}
</script>
</body></html>"""
        return web.Response(text=page, content_type="text/html")


def _jwt_sub(id_token: str) -> str | None:
    """Read the Cognito user id ('sub') from a JWT id_token without verifying it
    (we already trust these tokens — this is only to match sibling entries of the
    same account for multi-mower token propagation). Returns None on any problem."""
    try:
        import base64 as _b64, json as _json
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)          # restore base64 padding
        return _json.loads(_b64.urlsafe_b64decode(payload)).get("sub")
    except Exception:
        return None


def _thing_name(d: dict) -> str:
    return d.get("deviceThingName") or d.get("thingName") or d.get("thing_name") or d.get("deviceId") or d.get("id") or str(d)

def _label(d: dict) -> str:
    n = d.get("deviceName") or d.get("name") or d.get("alias") or _thing_name(d)
    return f"Lymow {n}"