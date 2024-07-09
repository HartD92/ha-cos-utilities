"""Colorado Springs Utilities API."""

import dataclasses
import json
import logging
from datetime import datetime
from datetime import timedelta
from enum import Enum
from typing import Any

import aiohttp
import arrow
from aiohttp.client_exceptions import ClientResponseError
from dateutil import tz

from .const import TIME_ZONE
from .const import USER_AGENT
from .exceptions import CannotConnect
from .exceptions import InvalidAuth

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class CsuCustomer:
    """Data about a customer."""

    customer_id: str
    customer_context: dict


class AggregateType(Enum):
    """How to aggregate historical data."""

    BILL = "bill"
    DAY = "day"
    HOUR = "hour"
    QUARTER = "quarter"

    def __str__(self):
        """Return the value of the enum."""
        return self.value


class MeterType(Enum):
    """Meter type. Electric, gas, or water."""

    ELEC = "ELEC"
    GAS = "GAS"
    WATER = "WATER"

    def __str__(self):
        """Return the value of the enum."""
        return self.value


class ReadResolution(Enum):
    """Minimum supported resolution."""

    BILLING = "BILLING"
    DAY = "DAY"
    HOUR = "HOUR"
    QUARTER_HOUR = "QUARTER_HOUR"

    def __str__(self):
        """Return the value of the enum."""
        return self.value


SUPPORTED_AGGREGATE_TYPES = {
    ReadResolution.BILLING: [AggregateType.BILL],
    ReadResolution.DAY: [AggregateType.BILL, AggregateType.DAY],
    ReadResolution.HOUR: [AggregateType.BILL, AggregateType.DAY, AggregateType.HOUR],
    ReadResolution.QUARTER_HOUR: [
        AggregateType.BILL,
        AggregateType.DAY,
        AggregateType.HOUR,
    ],
}


@dataclasses.dataclass
class Meter:
    """Data about a Meter."""

    customer: CsuCustomer
    meter_number: int
    service_number: str
    service_id: str
    contract_num: str
    meter_type: MeterType
    read_resolution: ReadResolution | None = None


@dataclasses.dataclass
class UsageRead:
    """A read from the meeter that has consumption data."""

    meter: Meter
    start_time: datetime
    end_time: datetime
    consumption: float


@dataclasses.dataclass
class CostRead:
    """A read from the meter that has both consumption and cost data."""

    start_time: datetime
    end_time: datetime
    consumption: float  # taken from value field, in KWH or CCF
    provided_cost: float  # in $


class CSU:
    """Class that can get historical usage/cost from Colorado Springs Utilities."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        """Initialize the class."""

        self.session = session
        self.username = username
        self.password = password
        self.access_token = ""
        self.customers = []
        self.meters = []

    async def async_login(self) -> None:
        """Login to the API."""

        customerId = ""
        data = aiohttp.FormData()
        data.add_field("username", self.username)
        data.add_field("password", self.password)
        data.add_field("mfa", "")
        data.add_field("grant_type", "password")
        try:
            async with self.session.post(
                "https://myaccount.csu.org/rest/oauth/token",
                data=data,
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": "Basic d2ViQ2xpZW50SWRQYXNzd29yZDpzZWNyZXQ=",
                },
                raise_for_status=True,
            ) as resp:
                result = await resp.json()
                if "errorMsg" in result:
                    raise InvalidAuth(result["errorMsg"])

                self.access_token = result["access_token"]
                customerId = result["user"]["customerId"]
                # self.customers.append(Customer(customerId=result['user']["customerId"]))

        except ClientResponseError as err:
            if err.status in (401, 403):
                raise InvalidAuth(err) from err

            raise CannotConnect(err) from err

        try:
            async with self.session.post(
                "https://myaccount.csu.org/rest/account/list/",
                data=json.dumps({"customerId": customerId}),
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": "Bearer " + self.access_token,
                    "Content-Type": "application/json",
                },
            ) as resp:
                result = await resp.json()
                if "errorMsg" in result:
                    raise InvalidAuth(result["errorMsg"])
                # customerId = customerId
                customerContext = result["account"][0]
                _LOGGER.info("Customer ID: %s", customerId)
                # _LOGGER.info("Customer Context: %s", customerContext)
                self.customers.append(
                    CsuCustomer(
                        customer_id=customerId, customer_context=customerContext
                    )
                )
                # _LOGGER.info("Customer: %s", self.customers[0])

        except ClientResponseError as err:
            if err.status in (401, 403):
                raise InvalidAuth(err) from err

            raise CannotConnect(err) from err

    async def async_get_meters(self) -> None:
        """Get meters for the customer."""

        try:
            async with self.session.post(
                "https://myaccount.csu.org/rest/account/services/",
                data=json.dumps(
                    {
                        "customerId": self.customers[0].customer_id,
                        "multiAcctLimit": 10,
                        "accountContext": self.customers[0].customer_context,
                    }
                ),
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": "Bearer " + self.access_token,
                    "Content-Type": "application/json",
                },
            ) as resp:
                result = await resp.json()
                if "errorMsg" in result:
                    raise InvalidAuth(result["errorMsg"])

                meterResult = result["accountSummaryType"]["servicesForGraph"]
                # _LOGGER.info("Meters: %s", meterResult)
                for meter in meterResult:
                    if meter["serviceNumber"] == "G-TYPICAL":
                        meterType = MeterType.GAS
                        readFrequency = ReadResolution.HOUR
                    elif meter["serviceNumber"] == "W-TYPICAL":
                        meterType = MeterType.WATER
                        readFrequency = ReadResolution.DAY
                    elif meter["serviceNumber"] == "E-TYPICAL":
                        meterType = MeterType.ELEC
                        readFrequency = ReadResolution.QUARTER_HOUR
                    self.meters.append(
                        Meter(
                            customer=self.customers[0],
                            meter_number=meter["meterNumber"],
                            service_number=meter["serviceNumber"],
                            service_id=meter["serviceId"],
                            contract_num=meter["serviceContract"],
                            meter_type=meterType,
                            read_resolution=readFrequency,
                        )
                    )
            # _LOGGER.info("Meters Object: %s", self.meters)
        except ClientResponseError as err:
            if err.status in (401, 403):
                raise InvalidAuth(err) from err
            raise CannotConnect(err) from err

    async def async_get_usage_reads(
        self,
        meter: Meter,
        aggregate_type: AggregateType,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[UsageRead]:
        """Get usage reads for a meter."""

        if not meter.read_resolution:
            raise ValueError("Meter does not have a read resolution")
        if aggregate_type not in SUPPORTED_AGGREGATE_TYPES[meter.read_resolution]:
            raise ValueError(f"Unsupported aggregate type for {meter.read_resolution}")
        if not start_date:
            raise ValueError("start_date is required")
        if not end_date:
            raise ValueError("end_date is required")
        if start_date > end_date:
            raise ValueError("start_date must be before end_date")

        reads = await self.async_get_dated_data(
            meter=meter,
            url="https://myaccount.csu.org/rest/usage/detail/",
            aggregate_type=aggregate_type,
            start_date=start_date,
            end_date=end_date,
        )
        if aggregate_type in {AggregateType.QUARTER, AggregateType.HOUR}:
            meterReadField = "readDateTime"
            consumptionField = "usageConsumptionValue"
        else:
            meterReadField = "readDate"
            consumptionField = "usageConsumptionValue"

        result = []
        readStart = None
        DEN = tz.gettz(TIME_ZONE)

        # Home Assistant recorder will only allow hourly updates. We aggregate the 15m reads to hourly here.
        if meter.meter_type == MeterType.ELEC and aggregate_type == AggregateType.HOUR:
            aggConsumption = 0.0
            for read in reads:
                if read[meterReadField] is not None:
                    if readStart is None:
                        readStart = datetime.fromisoformat(
                            read[meterReadField]
                        ).replace(tzinfo=DEN) - timedelta(hours=1)
                    if read[consumptionField] is not None:
                        aggConsumption = aggConsumption + read[consumptionField]
                    readEnd = datetime.fromisoformat(read[meterReadField]).replace(
                        tzinfo=DEN
                    )
                    _LOGGER.debug("Processing read from %s to %s", readStart, readEnd)
                    if readStart.minute == 45:
                        result.append(
                            UsageRead(
                                meter=meter,
                                start_time=readEnd - timedelta(hours=1),
                                end_time=readEnd,
                                consumption=aggConsumption,
                            )
                        )
                        _LOGGER.debug(
                            "Adding read for %s with value of %s",
                            (readEnd - timedelta(hours=1)),
                            aggConsumption,
                        )
                        aggConsumption = 0.0
                    readStart = datetime.fromisoformat(read[meterReadField])
        else:
            # TODO: Need to adjust the datetime for the daily reads, it's offset by 1 day.
            for read in reads:
                if read[meterReadField] is not None:
                    if readStart is None:
                        if aggregate_type == AggregateType.DAY:
                            readStart = datetime.fromisoformat(
                                read[meterReadField]
                            ).replace(tzinfo=DEN) - timedelta(days=1)
                        elif aggregate_type == AggregateType.HOUR:
                            readStart = datetime.fromisoformat(
                                read[meterReadField]
                            ).replace(tzinfo=DEN) - timedelta(hours=1)
                    elif aggregate_type == AggregateType.DAY:
                        readStart = readStart + timedelta(days=1)
                    result.append(
                        UsageRead(
                            meter=meter,
                            start_time=readStart.replace(tzinfo=DEN),
                            end_time=datetime.fromisoformat(
                                read[meterReadField]
                            ).replace(tzinfo=DEN),
                            consumption=read[consumptionField],
                        )
                    )
                    readStart = datetime.fromisoformat(read[meterReadField])
        return result

    async def async_get_dated_data(
        self,
        meter: Meter,
        url: str,
        aggregate_type: AggregateType,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[Any]:
        """Break large _async_fetch requests into smaller batches."""

        if start_date is None:
            raise ValueError("start_date is required")
        if end_date is None:
            raise ValueError("end_date is required")
        # DEN = tz.gettz(TIME_ZONE)

        start = start_date.date()
        end = end_date.date()
        # start = arrow.get(start_date.date(), TIME_ZONE)
        # end = arrow.get(end_date.date(), TIME_ZONE)

        max_request_days = 30
        if aggregate_type == AggregateType.DAY:
            max_request_days = 60
            url_end = "month"
        if aggregate_type in {AggregateType.QUARTER, AggregateType.HOUR}:
            max_request_days = 1
            url_end = "day"

        url = url + url_end

        result: list[Any] = []
        req_end = end
        while True:
            req_start = start
            if max_request_days is not None:
                req_start = max(start, (req_end - timedelta(days=max_request_days)))
            if req_start >= req_end:
                return result
            reads = await self._async_fetch(meter, url, req_start, req_end)
            if not reads:
                return result
            result = reads + result
            # req_end = req_start.shift(days=-1)
            req_end = req_end - timedelta(days=max_request_days)

    async def _async_fetch(
        self,
        meter: Meter,
        url: str,
        start_date: datetime | arrow.Arrow | None = None,
        end_date: datetime | arrow.Arrow | None = None,
    ) -> list[Any]:
        data = {
            "customerId": meter.customer.customer_id,
            "meterNumber": meter.meter_number,
            "serviceNumber": meter.service_number,
            "serviceId": meter.service_id,
            "accountContext": meter.customer.customer_context,
            "contractNum": meter.contract_num,
        }
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"

        if start_date:
            data["fromDate"] = start_date.strftime("%Y-%m-%d %H:%M")
        if end_date:
            data["toDate"] = end_date.strftime("%Y-%m-%d %H:%M")
        try:
            async with self.session.post(
                url, data=json.dumps(data), headers=headers, raise_for_status=True
            ) as resp:
                result = await resp.json()
                # if DEBUG_LOG_RESPONSE:
                #   _LOGGER.debug("Fetched: %s", json.dumps(result,indent=2))
                return result["history"]
        except ClientResponseError as err:
            # Ignore server errors for BILL requests
            # that can happen if end_date is before account activation
            if err.status == 500:
                return []
            raise

    def _get_headers(self):
        headers = {"User-Agent": USER_AGENT}
        if self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
        return headers
