"""Base entity for CSU Integration"""
from homeassistant.core import HomeAssistant  # type: ignore
from homeassistant.helpers.device_registry import DeviceInfo  # type: ignore
from homeassistant.helpers.update_coordinator import CoordinatorEntity  # type: ignore

from .const import DOMAIN
from .coordinator import CsuCoordinator


async def async_setup_entry(hass: HomeAssistant, config_entry):
    """Add sensors for passed config_entry in HA."""
    # coordinator = CsuCoordinator(hass, config_entry)


class CSUEntity(CoordinatorEntity[CsuCoordinator]):
    """Common entity class for all CSU entities"""

    def __init__(self, coordinator) -> None:
        """Initialize CSU Entity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.coordinator.entities.append(self)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, "CSU")
            },
            name="CSU",
            manufacturer="Colorado Springs Utilities",
            configuration_url="https://www.csu.org",
        )

    @property
    def should_poll(self) -> bool:
        return False
