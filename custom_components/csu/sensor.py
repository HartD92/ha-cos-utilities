"""Platform for sensor integration."""

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity  # type: ignore
from homeassistant.const import UnitOfEnergy, UnitOfVolume  # type: ignore
from homeassistant.core import HomeAssistant  # type: ignore
from homeassistant.helpers.typing import StateType  # type: ignore

from .const import DOMAIN
from .coordinator import CsuCoordinator
from .csu import MeterType
from .entity import CSUEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Add sensors for passed config_entry in HA."""
    coordinator = CsuCoordinator(hass, config_entry.data)
    if not coordinator.api.customers:
        await coordinator.api.async_login()
    if not coordinator.api.meters:
        await coordinator.api.async_get_meters()
    # 
    async_add_entities(
        CSUSensor(coordinator, meter.meter_type) for meter in coordinator.api.meters
    )

    async_add_entities(
        CSUCostSensor(coordinator, meter.meter_type) for meter in coordinator.api.meters
    )


class CSUSensor(CSUEntity, SensorEntity):
    """CSU Sensor Class."""

    def __init__(self, coordinator, service) -> None:
        """Initialize CSU Sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"csu_{service}_consumption"
        self.key = service.name
        self._attr_has_entity_name = True

        match service:
            case MeterType.ELEC:
                self._attr_device_class = SensorDeviceClass.ENERGY
                self._attr_last_reset = None
                self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
                self._attr_state_class = "total_increasing"
                self._attr_name = "Electricity Consumption"
            case MeterType.GAS:
                self._attr_device_class = SensorDeviceClass.GAS
                self._attr_last_reset = None
                self._attr_native_unit_of_measurement = UnitOfVolume.CENTUM_CUBIC_FEET
                self._attr_state_class = "total_increasing"
                self._attr_suggested_display_precision = 0
                self._attr_name = "Gas Consumption"
            case MeterType.WATER:
                self._attr_device_class = SensorDeviceClass.WATER
                self._attr_last_reset = None
                self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_FEET
                self._attr_state_class = "total_increasing"
                self._attr_suggested_display_precision = 0
                self._attr_name = "Water Consumption"

    @property
    def available(self) -> bool:
        """Return if available."""
        return True

    @property
    def native_value(self) -> StateType:
        """Return native value for entity."""
        value = self.coordinator.data.get("monthly_totals").get(self.key).get("usage")
        if value == "":
            value = None
        return value

class CSUCostSensor(CSUEntity, SensorEntity):
    """CSU Sensor Class."""

    def __init__(self, coordinator, service) -> None:
        """Initialize CSU Sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"csu_{service}_cost"
        self.key = service.name
        self._attr_has_entity_name = True

        match service:
            case MeterType.ELEC:
                self._attr_device_class = SensorDeviceClass.MONETARY
                self._attr_last_reset = None
                self._attr_native_unit_of_measurement = "USD"
                self._attr_state_class = "total"
                self._attr_name = "Electricity Cost"
            case MeterType.GAS:
                self._attr_device_class = SensorDeviceClass.MONETARY
                self._attr_last_reset = None
                self._attr_native_unit_of_measurement = "USD"
                self._attr_state_class = "total"
                self._attr_suggested_display_precision = 0
                self._attr_name = "Gas Cost"
            case MeterType.WATER:
                self._attr_device_class = SensorDeviceClass.MONETARY
                self._attr_last_reset = None
                self._attr_native_unit_of_measurement = "USD"
                self._attr_state_class = "total"
                self._attr_suggested_display_precision = 0
                self._attr_name = "Water Cost"

    @property
    def available(self) -> bool:
        """Return if available."""
        return True

    @property
    def native_value(self) -> StateType:
        """Return native value for entity."""
        value = self.coordinator.data.get("monthly_totals").get(self.key).get("cost")
        if value == "":
            value = None
        return value
