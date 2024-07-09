"""Coordinator for CSU data."""

import logging
from datetime import datetime
from datetime import timedelta
from types import MappingProxyType
from typing import Any
from typing import cast

from homeassistant.components.recorder import get_instance # type: ignore
from homeassistant.components.recorder.models import StatisticData # type: ignore
from homeassistant.components.recorder.models import StatisticMetaData # type: ignore
from homeassistant.components.recorder.statistics import async_add_external_statistics # type: ignore
from homeassistant.components.recorder.statistics import get_last_statistics # type: ignore
from homeassistant.components.recorder.statistics import statistics_during_period # type: ignore
from homeassistant.components.recorder.statistics import valid_statistic_id # type: ignore
from homeassistant.const import CONF_PASSWORD # type: ignore
from homeassistant.const import CONF_USERNAME # type: ignore
from homeassistant.const import UnitOfEnergy # type: ignore
from homeassistant.const import UnitOfVolume # type: ignore
from homeassistant.core import HomeAssistant # type: ignore
from homeassistant.core import callback # type: ignore
from homeassistant.exceptions import ConfigEntryAuthFailed # type: ignore
from homeassistant.helpers import aiohttp_client # type: ignore
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator # type: ignore
from homeassistant.util import dt as dt_util # type: ignore

from .const import DOMAIN
from .const import TIME_ZONE
from .csu import CSU
from .csu import AggregateType
from .csu import Meter
from .csu import MeterType
from .csu import ReadResolution
from .csu import UsageRead
from .exceptions import InvalidAuth

_LOGGER = logging.getLogger(__name__)


class CsuCoordinator(DataUpdateCoordinator[dict[str, UsageRead]]):
    """Handle fetching CSU data, updating sensors and inserting statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: MappingProxyType[str, Any],
    ) -> None:
        """Initialize the data handler."""

        super().__init__(
            hass,
            _LOGGER,
            name="Csu",
            update_interval=timedelta(hours=12),
        )
        self.api = CSU(
            aiohttp_client.async_get_clientsession(hass),
            entry_data[CONF_USERNAME],
            entry_data[CONF_PASSWORD],
        )
        self.entities = []
        self.data = {
            "monthly_totals": {
                "ELEC": {"usage": None},
                "GAS": {"usage": None},
                "WATER": {"usage": None},
            }
        }

        @callback
        def _dummy_listener() -> None:
            pass

        self.async_add_listener(_dummy_listener)

    async def _async_update_data(self):
        """Fetch data from CSU."""

        try:
            await self.api.async_login()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed from err
        # TODO: Usage Reads works great, but for useful sensors we need to take that data and get current bill usage.
        # Update config flow to add in a field for bill rollover date?
        # We can use that date to get everything since, and get current month usage.
        # If we poll last 12 months we can get 'typical' usage and cost.
        # That will let us make the sensors.
        # usage_reads: list[UsageRead] = await self.api.async_get_usage_reads()
        # _LOGGER.debug("Updating sensor data with: %s", usage_reads)
        if not self.api.meters:
            await self.api.async_get_meters()
        for meter in self.api.meters:
            start_time = datetime.today().replace(day=1)
            end_time = datetime.today()
            sum_usage = 0.0
            usage_reads = await self.api.async_get_usage_reads(meter, AggregateType.DAY, start_time, end_time)
            for usage_read in usage_reads:
                sum_usage += usage_read.consumption
            self.data["monthly_totals"][meter.meter_type.name]["usage"] = sum_usage

        await self._insert_statistics()

    async def _insert_statistics(self) -> None:
        """Insert CSU Statistics."""
        if not self.api.meters:
            await self.api.async_get_meters()
        for meter in self.api.meters:
            id_prefix = (
                f"csu_{meter.meter_type.name.lower()}_{meter.customer.customer_id}"
            )

            consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
            _LOGGER.info(
                "Updating Statistics for %s",
                consumption_statistic_id,
            )

            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
            )
            if not last_stat:
                _LOGGER.info("Updating statistics for the first time")
                usage_reads = await self._async_get_usage_reads(meter, TIME_ZONE)
                consumption_sum = 0.0
                last_stats_time = None
            else:
                usage_reads = await self._async_get_usage_reads(
                    meter,
                    TIME_ZONE,
                    last_stat[consumption_statistic_id][0]["start"],
                )
                if not usage_reads:
                    _LOGGER.debug("No recent usage data. Skipping update")
                    continue
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    usage_reads[0].start_time,
                    None,
                    {consumption_statistic_id},
                    (
                        "hour"
                        if meter.meter_type in (MeterType.ELEC, MeterType.GAS)
                        else "day"
                    ),
                    None,
                    {"sum"},
                )
                consumption_sum = cast(float, stats[consumption_statistic_id][0]["sum"])
                last_stats_time = stats[consumption_statistic_id][0]["start"]

            consumption_statistics = []

            for usage_read in usage_reads:
                start = usage_read.start_time
                if last_stats_time is not None and start.timestamp() <= last_stats_time:
                    continue
                consumption_sum += usage_read.consumption

                consumption_statistics.append(
                    StatisticData(
                        start=start, state=usage_read.consumption, sum=consumption_sum
                    )
                )

            name_prefix = (
                f"CSU {meter.meter_type.name.lower()} {meter.customer.customer_id}"
            )
            unit = None
            if meter.meter_type == MeterType.ELEC:
                unit = UnitOfEnergy.KILO_WATT_HOUR
            elif meter.meter_type == MeterType.GAS:
                unit = UnitOfVolume.CENTUM_CUBIC_FEET
            elif meter.meter_type == MeterType.WATER:
                unit = UnitOfVolume.CUBIC_FEET
            consumption_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"{name_prefix} consumption",
                source=DOMAIN,
                statistic_id=consumption_statistic_id,
                unit_of_measurement=unit,
            )
            if valid_statistic_id(consumption_statistic_id):
                _LOGGER.info("Statistic to insert data into: %s", consumption_metadata)
                # _LOGGER.debug("Stats Object: %s", consumption_statistics)
                async_add_external_statistics(
                    self.hass, consumption_metadata, consumption_statistics
                )
                continue
            _LOGGER.warning(
                "Invalid statistic id: %s",
                consumption_statistic_id,
            )

    async def _async_get_usage_reads(
        self, meter: Meter, time_zone_str: str, start_time: float | None = None
    ) -> list[UsageRead]:
        """Get usage reads.

        If start_time is None, get usage reads since account activation, otherwise since start_time - 30 days to allow corrections in data from utilities.

        We read at different resolutions depending on age:
        - day resolution for past 3 years
        - hour resolution for past 2 months
        """

        def _update_with_finer_usage_reads(
            usage_reads: list[UsageRead], finer_usage_reads: list[UsageRead]
        ) -> None:
            for i, usage_read in enumerate(usage_reads):
                for j, finer_usage_read in enumerate(finer_usage_reads):
                    _LOGGER.debug(
                        "Read end time is %s and fine read start time is %s",
                        usage_read.end_time,
                        finer_usage_read.start_time,
                    )
                    if usage_read.start_time == finer_usage_read.start_time:
                        usage_reads[i:] = finer_usage_reads[j:]
                        return
                    if usage_read.end_time == finer_usage_read.start_time:
                        usage_reads[i + 1 :] = finer_usage_reads[j:]
                        return
                    if usage_read.end_time < finer_usage_read.start_time:
                        break
            usage_reads += finer_usage_reads

        tz = await dt_util.async_get_time_zone(time_zone_str)
        if start_time is None:
            start = dt_util.now(tz) - timedelta(days=3 * 365)
        else:
            start = datetime.fromtimestamp(start_time, tz=tz) - timedelta(days=30)
        end = dt_util.now(tz)
        usage_reads = await self.api.async_get_usage_reads(
            meter, AggregateType.DAY, start, end
        )
        if meter.read_resolution == ReadResolution.DAY:
            _LOGGER.debug("Meter is Daily only. Returning Usages")
            return usage_reads

        if start_time is None:
            start = end - timedelta(days=2 * 30)
        else:
            assert start
            start = max(start, end - timedelta(days=2 * 30))
        _LOGGER.debug(
            "Meter is hourly. Getting finer data stats for %s until %s", start, end
        )
        hourly_usage_reads = await self.api.async_get_usage_reads(
            meter, AggregateType.HOUR, start, end
        )
        _update_with_finer_usage_reads(usage_reads, hourly_usage_reads)
        return usage_reads

        # add in additional update for quarter-hour reads?
