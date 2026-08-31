"""Conservative climate entities for Tapo AC IR profiles."""
from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .button import child_device_info
from .coordinator import TapoIrCoordinator

PARALLEL_UPDATES = 1

_HVAC_TO_TAPO = {
    HVACMode.COOL: 0,
    HVACMode.HEAT: 1,
    HVACMode.AUTO: 2,
    HVACMode.FAN_ONLY: 3,
    HVACMode.DRY: 4,
}
_TAPO_TO_HVAC = {value: key for key, value in _HVAC_TO_TAPO.items()}
_NON_TEMPERATURE_HVAC_MODES = {
    HVACMode.AUTO,
    HVACMode.FAN_ONLY,
    HVACMode.DRY,
}
_FAN_TO_TAPO = {
    "auto": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}
_TAPO_TO_FAN = {value: key for key, value in _FAN_TO_TAPO.items()}
_SWING_TO_TAPO = {
    "swing": (0, 6),
    "auto": (1, 7),
    "position_2": (2, 7),
    "position_3": (3, 7),
    "position_4": (4, 7),
    "position_5": (5, 7),
    "position_6": (6, 7),  # Experimental: not exposed by the Tapo app.
}
_TAPO_TO_SWING = {
    wind_direct: swing_mode
    for swing_mode, (wind_direct, _pressed_fid) in _SWING_TO_TAPO.items()
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create climate entities only for AC profiles."""
    coordinator: TapoIrCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_ac_remotes() -> None:
        entities: list[TapoIrAcClimate] = []
        for device in (coordinator.data or {}).values():
            if (
                device.get("model") != "AC"
                or device["device_id"] in known
            ):
                continue
            known.add(device["device_id"])
            entities.append(TapoIrAcClimate(coordinator, device))
        if entities:
            async_add_entities(entities)

    _add_new_ac_remotes()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_ac_remotes))


class TapoIrAcClimate(
    CoordinatorEntity[TapoIrCoordinator],
    ClimateEntity,
):
    """Optimistic state control for an IR-only AC profile."""

    _attr_has_entity_name = True
    _attr_translation_key = "air_conditioner"
    _attr_icon = "mdi:air-conditioner"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 16
    _attr_max_temp = 30
    _attr_target_temperature_step = 1
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.AUTO,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]
    _attr_fan_modes = list(_FAN_TO_TAPO)
    _attr_swing_modes = list(_SWING_TO_TAPO)
    _attr_supported_features = (
        ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
    )

    def __init__(
        self,
        coordinator: TapoIrCoordinator,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device["device_id"]
        self._attr_unique_id = f"{self._device_id}_climate"
        self._attr_device_info = child_device_info(coordinator, device)
        self._last_temperature: float | None = None
        self._remember_temperature()

    @property
    def _device(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get(self._device_id)

    @property
    def _state(self) -> dict[str, int]:
        return (self._device or {}).get("ac_state", {})

    def _remember_temperature(self) -> None:
        value = self._state.get("T")
        if value is not None and self.min_temp <= value <= self.max_temp:
            self._last_temperature = float(value)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember_temperature()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        return super().available and self._device is not None

    @property
    def hvac_mode(self) -> HVACMode:
        if self._state.get("P", 0) == 0:
            return HVACMode.OFF
        return _TAPO_TO_HVAC.get(self._state.get("M", 0), HVACMode.COOL)

    @property
    def hvac_action(self) -> HVACAction:
        return {
            HVACMode.OFF: HVACAction.OFF,
            HVACMode.COOL: HVACAction.COOLING,
            HVACMode.HEAT: HVACAction.HEATING,
            HVACMode.AUTO: HVACAction.IDLE,
            HVACMode.FAN_ONLY: HVACAction.FAN,
            HVACMode.DRY: HVACAction.DRYING,
        }[self.hvac_mode]

    @property
    def target_temperature(self) -> float | None:
        value = self._state.get("T")
        if value is not None and self.min_temp <= value <= self.max_temp:
            return float(value)
        return self._last_temperature

    @property
    def current_temperature(self) -> None:
        """IR profiles do not measure ambient temperature."""
        return None

    @property
    def fan_mode(self) -> str | None:
        return _TAPO_TO_FAN.get(self._state.get("S"))

    @property
    def swing_mode(self) -> str | None:
        return _TAPO_TO_SWING.get(self._state.get("D"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Clarify that state is last-command state, not device feedback."""
        return {
            "state_source": "last_known_ir_profile",
            "raw_tapo_state": dict(self._state),
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode is HVACMode.OFF:
            await self.coordinator.async_control_ac(
                self._device_id,
                power=False,
                pressed_fid=1,
            )
            return
        if hvac_mode not in _HVAC_TO_TAPO:
            raise HomeAssistantError(f"Unsupported AC mode: {hvac_mode}")

        changes: dict[str, Any] = {
            "power": True,
            "mode": _HVAC_TO_TAPO[hvac_mode],
        }
        if hvac_mode in _NON_TEMPERATURE_HVAC_MODES:
            self._remember_temperature()
            changes["temp"] = -1
        else:
            self._remember_temperature()
            if self._last_temperature is None:
                raise HomeAssistantError(
                    "No previous target temperature is available for this AC"
                )
            changes["temp"] = round(self._last_temperature)

        await self.coordinator.async_control_ac(
            self._device_id,
            pressed_fid=2,
            **changes,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_ac(
            self._device_id,
            power=True,
            pressed_fid=1,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_ac(
            self._device_id,
            power=False,
            pressed_fid=1,
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            raise HomeAssistantError("A target temperature is required")
        rounded = round(float(temperature))
        if not self.min_temp <= rounded <= self.max_temp:
            raise HomeAssistantError(
                f"Target temperature must be {self.min_temp}-{self.max_temp} °C"
            )
        self._last_temperature = float(rounded)
        if self.hvac_mode in _NON_TEMPERATURE_HVAC_MODES:
            self.async_write_ha_state()
            return
        await self.coordinator.async_control_ac(
            self._device_id,
            temp=rounded,
            pressed_fid=3,
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in _FAN_TO_TAPO:
            raise HomeAssistantError(f"Unsupported fan mode: {fan_mode}")
        await self.coordinator.async_control_ac(
            self._device_id,
            wind_speed=_FAN_TO_TAPO[fan_mode],
            pressed_fid=5,
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if swing_mode not in _SWING_TO_TAPO:
            raise HomeAssistantError(f"Unsupported swing mode: {swing_mode}")
        wind_direct, pressed_fid = _SWING_TO_TAPO[swing_mode]
        await self.coordinator.async_control_ac(
            self._device_id,
            wind_direct=wind_direct,
            pressed_fid=pressed_fid,
        )
