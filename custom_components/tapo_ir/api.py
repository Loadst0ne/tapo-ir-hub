"""Async direct-access client for a Tapo H1xx IR hub."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import logging
from typing import Any

from .ac import AcStateError, build_ac_payload, parse_ac_status
from .compat import (
    AuthCredential,
    DeviceConnectConfiguration,
    TapoRequest,
    connect,
)
from .const import IR_CATEGORY
from .naming import (
    humanize_key_label,
    humanize_remote_name,
    slugify,
)
from .protocol import ProtocolResponseError, validate_protocol_response

_LOGGER = logging.getLogger(__name__)


class TapoIrError(Exception):
    """Base error for the Tapo IR API."""


class TapoIrAuthError(TapoIrError):
    """Raised when the hub rejects supplied credentials."""


class TapoIrConnectionError(TapoIrError):
    """Raised when the hub cannot be reached or a request fails."""


def parse_child_devices(
    raw: dict[str, Any],
    overrides: dict[str, str] | None = None,
    *,
    include_codes: bool = False,
) -> list[dict[str, Any]]:
    """Normalize child remotes and their key metadata."""
    devices: list[dict[str, Any]] = []
    for child in raw.get("child_device_list", []):
        if child.get("category") != IR_CATEGORY or not child.get("device_id"):
            continue

        device_id = str(child["device_id"])
        key_list = child.get("key_list") or []
        remote_name = humanize_remote_name(
            device_id,
            child.get("nickname"),
            [str(key.get("name", "")) for key in key_list],
            overrides,
        )
        keys: list[dict[str, Any]] = []
        used_labels: dict[str, int] = {}
        used_legacy_slugs: dict[str, int] = {}
        for position, key in enumerate(key_list, start=1):
            protocol_name = str(key.get("name") or f"key_{position}")
            label, label_source = humanize_key_label(
                protocol_name, key.get("display_name"), position
            )
            label_key = label.casefold()
            used_labels[label_key] = used_labels.get(label_key, 0) + 1
            if used_labels[label_key] > 1:
                label = f"{label} ({used_labels[label_key]})"
            raw_display_name = key.get("display_name", "")
            try:
                legacy_label = (
                    base64.b64decode(raw_display_name).decode("utf-8")
                    or protocol_name
                )
            except Exception:
                legacy_label = raw_display_name or protocol_name
            legacy_slug = slugify(legacy_label)
            used_legacy_slugs[legacy_slug] = (
                used_legacy_slugs.get(legacy_slug, 0) + 1
            )
            if used_legacy_slugs[legacy_slug] > 1:
                legacy_slug = (
                    f"{legacy_slug}_{used_legacy_slugs[legacy_slug]}"
                )
            parsed_key: dict[str, Any] = {
                "name": protocol_name,
                "id": key.get("id"),
                "label": label,
                "label_source": label_source,
                "slug": slugify(label),
                "legacy_slug": legacy_slug,
                "icon": pick_icon(label),
                "order": key.get("order", position),
                "type": key.get("type"),
            }
            if include_codes:
                parsed_key.update(
                    {
                        "display_name": key.get("display_name"),
                        "pwm": key.get("pwm"),
                        "pulse": key.get("pulse"),
                    }
                )
            keys.append(parsed_key)

        device: dict[str, Any] = {
            "device_id": device_id,
            "name": remote_name,
            "slug": slugify(remote_name),
            "model": child.get("model"),
            "category": child.get("category"),
            "key_count": child.get("key_sum", len(keys)),
            "keys": keys,
        }
        if child.get("model") == "AC":
            device["ac_state"] = parse_ac_status(child)
        devices.append(device)

    devices.sort(key=lambda device: device["name"].casefold())
    return devices


_ICON_HINTS: tuple[tuple[str, str], ...] = (
    ("power", "mdi:power"),
    ("mute", "mdi:volume-mute"),
    ("temperature up", "mdi:thermometer-plus"),
    ("temperature down", "mdi:thermometer-minus"),
    ("cool", "mdi:snowflake"),
    ("heat", "mdi:fire"),
    ("fan", "mdi:fan"),
    ("source", "mdi:import"),
    ("input", "mdi:import"),
    ("settings", "mdi:cog"),
    ("menu", "mdi:menu"),
    ("back", "mdi:arrow-left-circle"),
    ("return", "mdi:arrow-left-circle"),
    ("ok", "mdi:checkbox-marked-circle"),
    ("up", "mdi:chevron-up"),
    ("down", "mdi:chevron-down"),
    ("left", "mdi:chevron-left"),
    ("right", "mdi:chevron-right"),
    ("volume up", "mdi:volume-plus"),
    ("volume down", "mdi:volume-minus"),
    ("channel up", "mdi:plus"),
    ("channel down", "mdi:minus"),
)


def pick_icon(label: str) -> str:
    """Choose a conservative Material Design icon from a normalized label."""
    lowered = label.casefold()
    for hint, icon in _ICON_HINTS:
        if lowered == hint or hint in lowered.split():
            return icon
    return "mdi:remote"


class TapoIrApi:
    """Own a direct plugp100 connection and expose the integration API."""

    identifier_domain = "tapo_ir"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        overrides: dict[str, str] | None = None,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self.overrides = overrides or {}
        self._device: Any = None
        self._lock = asyncio.Lock()
        self._explicit_h110_profile = False
        self._raw_children: list[dict[str, Any]] = []
        self.hub_id: str | None = None
        self.hub_name = "Tapo IR Hub"
        self.hub_model: str | None = None
        self.hub_mac: str | None = None
        self.hub_fw: str | None = None

    @property
    def host(self) -> str:
        """Return the hub host."""
        return self._host

    def _connection_config(self) -> Any:
        kwargs: dict[str, Any] = {
            "host": self._host,
            "credentials": AuthCredential(self._username, self._password),
        }
        if self._explicit_h110_profile:
            kwargs.update(
                {
                    "device_type": "SMART.TAPOHUB",
                    "device_model": "H110",
                    "encryption_type": "KLAP",
                    "encryption_version": 2,
                }
            )
        return DeviceConnectConfiguration(**kwargs)

    async def _get_client(self) -> Any:
        if self._device is None:
            self._device = await connect(self._connection_config())
        return self._device.client

    async def _async_drop_device(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            try:
                await device.client.close()
            except (OSError, RuntimeError, AttributeError):
                _LOGGER.debug("Failed to close stale plugp100 client", exc_info=True)

    async def _request(
        self, request: TapoRequest, *, attempts: int = 3
    ) -> dict[str, Any]:
        """Execute a request, reconnecting after a stale KLAP session."""
        async with self._lock:
            last_error: Exception | None = None
            for _attempt in range(attempts):
                try:
                    client = await self._get_client()
                    response = await client.execute_raw_request(request)
                    if response.is_success():
                        result = response.get()
                        return result if isinstance(result, dict) else {"result": result}
                    last_error = TapoIrConnectionError(str(response.error()))
                except Exception as err:  # plugp100 exposes version-specific errors
                    last_error = err
                await self._async_drop_device()
            raise TapoIrConnectionError(str(last_error)) from last_error

    async def async_connect(self) -> None:
        """Validate the connection and capture hub identity."""
        try:
            info = await self._request(TapoRequest.get_device_info())
        except TapoIrConnectionError as first_error:
            message = str(first_error).casefold()
            if any(token in message for token in ("auth", "credential", "password", "1501")):
                raise TapoIrAuthError(str(first_error)) from first_error

            # Some H110 EU firmware cannot be auto-detected. Retry once with the
            # explicit profile confirmed by affected users in issue #1.
            self._explicit_h110_profile = True
            try:
                info = await self._request(TapoRequest.get_device_info())
            except TapoIrConnectionError as second_error:
                message = str(second_error).casefold()
                if any(
                    token in message
                    for token in ("auth", "credential", "password", "1501")
                ):
                    raise TapoIrAuthError(str(second_error)) from second_error
                raise second_error from first_error

        self.hub_id = str(info.get("device_id") or info.get("mac") or self._host)
        self.hub_mac = info.get("mac")
        self.hub_model = info.get("model")
        self.hub_fw = info.get("fw_ver") or info.get("hw_ver")
        nickname = humanize_remote_name(
            self.hub_id, info.get("nickname"), [], None
        )
        if not nickname.startswith("IR Remote "):
            self.hub_name = nickname

    async def async_get_raw_devices(self) -> list[dict[str, Any]]:
        """Read and cache the hub's raw IR child records."""
        raw = await self._request(TapoRequest.get_child_device_list(0))
        self._raw_children = deepcopy(raw.get("child_device_list") or [])
        return deepcopy(self._raw_children)

    async def async_enumerate(
        self, *, include_codes: bool = False
    ) -> list[dict[str, Any]]:
        """Return every normalized child IR remote."""
        children = await self.async_get_raw_devices()
        return parse_child_devices(
            {"child_device_list": children},
            self.overrides,
            include_codes=include_codes,
        )

    async def async_query_hub(
        self, method: str, params: Any = None
    ) -> dict[str, Any]:
        """Run a raw hub method through the existing direct session."""
        attempts = 3 if method.startswith("get") else 1
        response = await self._request(
            TapoRequest(method=method, params=params),
            attempts=attempts,
        )
        try:
            validate_protocol_response(response, method)
        except ProtocolResponseError as err:
            raise TapoIrConnectionError(str(err)) from err
        if method in response and isinstance(response[method], dict):
            return response[method]
        return response

    async def async_query_child(
        self,
        device_id: str,
        method: str,
        params: Any = None,
        *,
        batched: bool = False,
    ) -> dict[str, Any]:
        """Run a raw method against one IR child."""
        request = TapoRequest(method=method, params=params)
        if batched:
            request = TapoRequest(
                method="multipleRequest",
                params={"requests": [{"method": method, "params": params}]},
            )
        attempts = 3 if method.startswith("get") else 1
        response = await self._request(
            TapoRequest.control_child(device_id, request),
            attempts=attempts,
        )
        try:
            validate_protocol_response(response, method)
        except ProtocolResponseError as err:
            raise TapoIrConnectionError(str(err)) from err
        if method in response and isinstance(response[method], dict):
            return response[method]
        return response

    async def async_fire(self, device_id: str, key_name: str) -> dict[str, Any]:
        """Fire a single stored IR key."""
        return await self.async_query_child(
            device_id,
            "sendIrCmdById",
            {"name": key_name},
            batched=True,
        )

    async def async_control_ac(
        self,
        device_id: str,
        *,
        current_state: dict[str, int],
        pressed_fid: int | None = None,
    ) -> dict[str, Any]:
        """Send one complete AC state through the remote profile."""
        try:
            payload = build_ac_payload(current_state, pressed_fid=pressed_fid)
        except AcStateError as err:
            raise TapoIrConnectionError(str(err)) from err
        return await self.async_query_child(
            device_id, "sendIrCmdByStatus", payload, batched=True
        )

    async def async_close(self) -> None:
        """Close the direct client."""
        async with self._lock:
            await self._async_drop_device()
