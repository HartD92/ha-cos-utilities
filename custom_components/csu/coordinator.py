"""Coordinator for CSU data."""
import logging
from datetime import datetime
from datetime import timedelta
from types import MappingProxyType
from typing import Any
from typing import cast

from homeassistant.components.recorder import get_instance  # type: ignore
from homeassistant.components.recorder.models import StatisticData  # type: ignore
from homeassistant.components.recorder.models import StatisticMetaData  # type: ignore
from homeassistant.util.unit_conversion import EnergyConverter  # type: ignore
from homeassistant.util.unit_conversion import VolumeConverter  # type: ignore

try:  # HA >= 2025.x replaced has_mean with mean_type and added unit_class
    from homeassistant.components.recorder.models import StatisticMeanType  # type: ignore
except ImportError:  # pragma: no cover - older Home Assistant
    StatisticMeanType = None
from homeassistant.components.recorder.statistics import async_add_external_statistics  # type: ignore
from homeassistant.components.recorder.statistics import get_last_statistics  # type: ignore
from homeassistant.components.recorder.statistics import statistics_during_period  # type: ignore
from homeassistant.components.recorder.statistics import valid_statistic_id  # type: ignore
from homeassistant.const import CONF_PASSWORD  # type: ignore
from homeassistant.const import CONF_USERNAME  # type: ignore
from homeassistant.const import UnitOfEnergy  # type: ignore
from homeassistant.const import UnitOfVolume  # type: ignore
from homeassistant.core import callback  # type: ignore
from homeassistant.core import HomeAssistant  # type: ignore
from homeassistant.exceptions import ConfigEntryAuthFailed  # type: ignore
from homeassistant.helpers import aiohttp_client  # type: ignore
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator  # type: ignore
from homeassistant.util import dt as dt_util  # type: ignore

from .const import DOMAIN
from .const import TIME_ZONE
from .csu import AggregateType
from .csu import CSU
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
            update_interval=timedelta(
                minutes=5
            ),  # TODO: Change to 12 Hours when testing is done.
        )
        self.api = CSU(
            aiohttp_client.async_get_clientsession(hass),
            entry_data[CONF_USERNAME],
            entry_data[CONF_PASSWORD],
        )
        self.entities = []
        self.data = {
            "monthly_totals": {
                "ELEC": {"usage": None, "cost": None},
                "GAS": {"usage": None, "cost": None},
                "WATER": {"usage": None, "cost": None},
            }
        }

        @callback
        def _dummy_listener() -> None:
            pass

        self.async_add_listener(_dummy_listener)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from CSU."""

        try:
            await self.api.async_login()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed from err
        # TODO: Usage Reads works great, but for useful sensors we need to take that data and get current bill usage.
        # If we poll last 12 months we can get 'typical' usage and cost.
        # That will let us make the sensors.
        # usage_reads: list[UsageRead] = await self.api.async_get_usage_reads()
        # _LOGGER.debug("Updating sensor data with: %s", usage_reads)
        if not self.api.meters:
            await self.api.async_get_meters()
        for meter in self.api.meters:
            bill_reads = await self.api.async_get_bill_history(meter)
            last_read = bill_reads.pop()
            # start_time = datetime.today().replace(day=1)
            start_time = last_read.end_time + timedelta(days=1)
            end_time = datetime.today()
            sum_usage = 0.0

            usage_reads = await self.api.async_get_usage_reads(
                meter, AggregateType.DAY, start_time, end_time
            )
            for usage_read in usage_reads:
                sum_usage += usage_read.consumption
            _LOGGER.debug(
                "Updating montly total data for %s with: %s",
                meter.meter_type.name,
                sum_usage,
            )
            self.data["monthly_totals"][meter.meter_type.name]["usage"] = sum_usage
            match meter.meter_type:
                case MeterType.ELEC:
                    total_cost = sum_usage * self.config_entry.options.get(
                        "electric_rate_per_kwh", 0.0
                    )
                    total_cost = total_cost + (
                        len(usage_reads)
                        * self.config_entry.options.get("electric_rate_per_day", 0.0)
                    )
                case MeterType.GAS:
                    total_cost = sum_usage * self.config_entry.options.get(
                        "gas_rate_per_ccf", 0.0
                    )
                    total_cost = total_cost + (
                        len(usage_reads)
                        * self.config_entry.options.get("gas_rate_per_day", 0.0)
                    )
                case MeterType.WATER:
                    total_cost = sum_usage * self.config_entry.options.get(
                        "water_rate_per_cf", 0.0
                    )
                    total_cost = total_cost + (
                        len(usage_reads)
                        * self.config_entry.options.get("water_rate_per_day", 0.0)
                    )
            self.data["monthly_totals"][meter.meter_type.name]["cost"] = total_cost
        # The first import walks ~3 years of history; never block HA startup on it.
        if not getattr(self, "_stats_task", None) or self._stats_task.done():
            self._stats_task = self.hass.async_create_background_task(
                self._insert_statistics(), "csu_insert_statistics"
            )
        return self.data

    async def _insert_statistics(self) -> None:
        """Insert CSU statistics; net-metered electric also gets an export series."""
        if not self.api.meters:
            await self.api.async_get_meters()
        for meter in self.api.meters:
            id_prefix = (
                f"csu_{meter.meter_type.name.lower()}_{meter.customer.customer_id}"
            )
            series = [("energy_consumption", "consumption", "consumption")]
            if meter.meter_type == MeterType.ELEC:
                series.append(("energy_production", "received", "returned to grid"))
            self._reads_cache = {}
            for suffix, attr, label in series:
                await self._insert_series(meter, id_prefix, suffix, attr, label)

    async def _cached_reads(self, meter, start_time):
        cache = getattr(self, "_reads_cache", {})
        # None (full history) satisfies any later-start request for the same meter
        for (sid, st), reads in cache.items():
            if sid == meter.service_id and (st is None or (start_time is not None and st <= start_time)):
                return reads
        reads = await self._async_get_usage_reads(meter, TIME_ZONE, start_time)
        cache[(meter.service_id, start_time)] = reads
        self._reads_cache = cache
        return reads

    async def _insert_series(self, meter, id_prefix, suffix, attr, label) -> None:
        statistic_id = f"{DOMAIN}:{id_prefix}_{suffix}"
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, set()
        )
        if not last_stat:
            _LOGGER.info("Updating statistics for the first time: %s", statistic_id)
            usage_reads = await self._cached_reads(meter, None)
            running_sum = 0.0
            last_stats_time = None
        else:
            usage_reads = await self._cached_reads(meter, last_stat[statistic_id][0]["start"])
            if not usage_reads:
                return
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                usage_reads[0].start_time,
                None,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            if stats.get(statistic_id):
                running_sum = cast(float, stats[statistic_id][0]["sum"])
                last_stats_time = stats[statistic_id][0]["start"]
            else:
                running_sum = cast(float, last_stat[statistic_id][0]["sum"])
                last_stats_time = last_stat[statistic_id][0]["start"]
        points = []
        for usage_read in usage_reads:
            start = usage_read.start_time
            if last_stats_time is not None and start.timestamp() <= last_stats_time:
                continue
            value = getattr(usage_read, attr)
            running_sum += value
            points.append(StatisticData(start=start, state=value, sum=running_sum))
        unit = {
            MeterType.ELEC: UnitOfEnergy.KILO_WATT_HOUR,
            MeterType.GAS: UnitOfVolume.CENTUM_CUBIC_FEET,
            MeterType.WATER: UnitOfVolume.CUBIC_FEET,
        }[meter.meter_type]
        metadata = StatisticMetaData(
            has_sum=True,
            name=f"CSU {meter.meter_type.name.lower()} {label}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=unit,
        )
        if StatisticMeanType is not None:
            metadata["mean_type"] = StatisticMeanType.NONE
            metadata["unit_class"] = (
                EnergyConverter.UNIT_CLASS
                if meter.meter_type == MeterType.ELEC
                else VolumeConverter.UNIT_CLASS
            )
        else:
            metadata["has_mean"] = False
        if not valid_statistic_id(statistic_id):
            _LOGGER.warning("Invalid statistic id: %s", statistic_id)
            return
        _LOGGER.info("Inserting %d points into %s", len(points), statistic_id)
        async_add_external_statistics(self.hass, metadata, points)

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
