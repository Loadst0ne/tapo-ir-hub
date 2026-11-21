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
    HVACMode.FAN_ONLY: 3,
    HVACMode.DRY: 4,
}
_TAPO_TO_HVAC = {value: key for key, value in _HVAC_TO_TAPO.items()}
_FAN_TO_TAPO = {"auto": 0, "low": 1, "high": 3}
_TAPO_TO_FAN = {value: key for key, value in _FAN_TO_TAPO.items()}
_SWING_TO_TAPO = {"auto": 0, "fixed": 1}
_TAPO_TO_SWING = {value: key for key, value in _SWING_TO_TAPO.items()}


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
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY]
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

    @property
    def _device(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get(self._device_id)

    @property
    def _state(self) -> dict[str, int]:
        return (self._device or {}).get("ac_state", {})

    @property
    def available(self) -> bool:
        return super().available and self._device is not None

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self._state.get("P") == 0:
            return HVACMode.OFF
        if self._state.get("P") != 1:
            return None
        return _TAPO_TO_HVAC.get(self._state.get("M"))

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.hvac_mode is None:
            return None
        if self.hvac_mode is HVACMode.OFF:
            return HVACAction.OFF
        if self.hvac_mode is HVACMode.HEAT:
            return HVACAction.HEATING
        if self.hvac_mode is HVACMode.COOL:
            return HVACAction.COOLING
        if self.hvac_mode is HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if self.hvac_mode is HVACMode.DRY:
            return HVACAction.DRYING
        return None

    @property
    def target_temperature(self) -> float | None:
        value = self._state.get("T")
        return float(value) if value is not None else None

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
                self._device_id, power=False
            )
            return
        if hvac_mode not in _HVAC_TO_TAPO:
            raise HomeAssistantError(f"Unsupported AC mode: {hvac_mode}")
        await self.coordinator.async_control_ac(
            self._device_id,
            power=True,
            mode=_HVAC_TO_TAPO[hvac_mode],
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_ac(self._device_id, power=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_ac(self._device_id, power=False)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            raise HomeAssistantError("A target temperature is required")
        rounded = round(float(temperature))
        if not self.min_temp <= rounded <= self.max_temp:
            raise HomeAssistantError(
                f"Target temperature must be {self.min_temp}-{self.max_temp} °C"
            )
        changes: dict[str, Any] = {"temp": rounded}
        if (hvac_mode := kwargs.get("hvac_mode")) is not None:
            if hvac_mode == HVACMode.OFF:
                changes["power"] = False
            elif hvac_mode in _HVAC_TO_TAPO:
                changes.update(power=True, mode=_HVAC_TO_TAPO[hvac_mode])
            else:
                raise HomeAssistantError(f"Unsupported AC mode: {hvac_mode}")
        await self.coordinator.async_control_ac(self._device_id, **changes)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in _FAN_TO_TAPO:
            raise HomeAssistantError(f"Unsupported fan mode: {fan_mode}")
        await self.coordinator.async_control_ac(
            self._device_id, wind_speed=_FAN_TO_TAPO[fan_mode]
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if swing_mode not in _SWING_TO_TAPO:
            raise HomeAssistantError(f"Unsupported swing mode: {swing_mode}")
        await self.coordinator.async_control_ac(
            self._device_id, wind_direct=_SWING_TO_TAPO[swing_mode]
        )
