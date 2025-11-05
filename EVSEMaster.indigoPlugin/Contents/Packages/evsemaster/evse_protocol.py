"""Simple EVSE protocol implementation for Home Assistant integration."""

import logging
import struct
from typing import Any, Optional
from datetime import datetime
import asyncio
import zoneinfo
from .data_types import (
    CommandEnum,
    DataPacket,
    EvseDeviceInfo,
    EvseStatus,
    ChargingStatus,
    CurrentStateEnum,
    NotLoggedInError,
)

log = logging.getLogger(__name__)


class _EVSEDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, parent: "SimpleEVSEProtocol"):
        self.parent = parent

    def datagram_received(self, data: bytes, addr):  # type: ignore[override]
        try:
            asyncio.create_task(self.parent._on_datagram(data, addr))
        except Exception as e:
            log.error(f"Datagram handling error: {e}")

    def error_received(self, exc):  # type: ignore[override]
        log.error(f"Datagram error received: {exc}")

    def connection_lost(self, exc):  # type: ignore[override]
        log.info("Datagram connection lost")
        self.parent._logged_in = False
        self.parent._transport = None


class SimpleEVSEProtocol:
    """Simple implementation of EVSE protocol for HA integration."""

    def __init__(self, host: str, password: str, event_callback: callable = None):
        """Initialize protocol handler."""
        self.host = host
        self.password = password
        self._event_callback = event_callback
        self.listen_port = 28376  # Port to listen for incoming datagrams
        self.send_port = 7248  # Default port to send to (will be updated by discovery)
        self.user_id = "evsemasterpy"  # Do all actions as this "user"
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._login_future: Optional[asyncio.Future] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._logged_in = False
        self._login_attempts = 0
        self._status: Optional[EvseStatus] = None
        self._device_info: Optional[EvseDeviceInfo] = None
        self._charging_status: Optional[ChargingStatus] = None
        self._discovery_running = False

    async def send_packet(self, data: bytes):
        if not self._transport:
            log.error("Transport not ready")
            return
        try:
            self._transport.sendto(data, (self.host, self.send_port))
        except Exception as e:
            log.error(f"Failed to send packet: {e}")

    async def connect(self) -> bool:
        """Create datagram endpoint and prepare transport."""
        try:
            loop = asyncio.get_running_loop()
            self._transport, protocol = await loop.create_datagram_endpoint(
                lambda: _EVSEDatagramProtocol(self), local_addr=("0.0.0.0", self.listen_port)
            )
            log.info("Datagram endpoint ready (listening %s:%d)", "0.0.0.0", self.listen_port)
            return True
        except Exception as err:
            log.error(f"Failed to create datagram endpoint: {err}")
            return False

    async def disconnect(self):
        """Close transport."""
        if self._transport:
            self._transport.close()
            self._transport = None
        self._logged_in = False
        self._login_future = None
        self._pending.clear()

    async def login(self) -> bool:
        if self._logged_in:
            await self.disconnect()
        if not self._transport:
            if not await self.connect():
                return False
        # Prepare future
        loop = asyncio.get_running_loop()
        self._login_future = loop.create_future()
        try:
            # as first action we probe to get a response/port number
            await self.send_packet(self._build_packet(CommandEnum.LOGIN_REQUEST))
            # wait for a short moment for transport setup and response from above
            await asyncio.sleep(5)
            await self.send_packet(self._build_packet(CommandEnum.LOGIN_REQUEST))
            await asyncio.wait_for(self._login_future, timeout=10)
            await self.request_essentials()
            return True
        except asyncio.TimeoutError:
            log.warning("Login timeout")
            return False
        except Exception as e:
            log.error(f"Login failed: {e}")
            return False
        finally:
            if self._login_future and not self._login_future.done():
                self._login_future.cancel()

    def send_event(self, event_type: str, data: Any):
        """Handle events from the EVSE."""
        if self._event_callback:
            try:
                self._event_callback(event_type, data)
            except Exception as e:
                log.error(f"Error in event callback: {e}")

    async def _on_datagram(self, data: bytes, addr):
        # Adjust discovered port if needed
        if addr[0] == self.host and addr[1] != self.send_port:
            log.debug(f"Discovered/updated EVSE port {addr[1]} (was {self.send_port})")
            self.send_port = addr[1]
        try:
            packet = DataPacket(data)
        except ValueError as e:
            log.warning(f"Invalid data packet received: {e}")
            return
        cmd = packet.command
        # Handle login success
        if cmd == CommandEnum.LOGIN_SUCCESS_EVENT:
            self._parse_device_info(packet)
            await self.send_packet(self._build_packet(CommandEnum.LOGIN_CONFIRM_RESPONSE))
            if self._login_future and not self._login_future.done():
                self._login_future.set_result(True)
            self._logged_in = True
        elif cmd == CommandEnum.PASSWORD_ERROR_EVENT:
            self._logged_in = False
            # try to re-login
            if self._login_future and not self._login_future.done():
                self._login_future.set_result(False)
            log.error("Password error")
            return
        elif cmd == CommandEnum.HEADING_EVENT:
            # Keepalive response
            asyncio.create_task(self.send_packet(self._build_packet(CommandEnum.HEADING_RESPONSE)))
        elif cmd == CommandEnum.CURRENT_STATUS_EVENT:
            log.debug(f"Current Status: \r\n {self._parse_status_response(packet)}")
            asyncio.create_task(self.send_packet(self._build_packet(CommandEnum.CURRENT_STATUS_RESPONSE)))
            self.send_event(EvseStatus.__name__, self._status)
        elif cmd == CommandEnum.CURRENT_CHARGING_STATUS_EVENT:
            log.debug(f"Charging Status: \r\n {self._parse_current_charging_status(packet)}")
            self.send_event(ChargingStatus.__name__, self._charging_status)
        elif cmd == CommandEnum.NICKNAME_EVENT:
            nickname = packet.get_string(1, packet.length() - 1)
            self._device_info.nickname = nickname
            log.debug(f"Nickname: {nickname}")
            self.send_event(EvseDeviceInfo.__name__, self._device_info)
        elif cmd == CommandEnum.OUTPUT_AMPERAGE_EVENT:
            amperage = packet.get_int(1, 1)
            self._device_info.configured_max_amps = amperage
            log.debug(f"Configured Max Amps: {amperage}A")
            self.send_event(EvseDeviceInfo.__name__, self._device_info)
        elif cmd == CommandEnum.NOT_LOGGED_IN_EVENT:
            # After sending a LOGIN_CONFIRM_RESPONSE we get one of these.
            # so we can ignore the first few
            self._login_attempts += 1
            if self._login_attempts > 3:
                log.warning("Received too many NOT_LOGGED_IN_EVENT, marking as logged out")
                self._logged_in = False
        else:
            log.debug(f"Unhandled command: {cmd.name}")
        if cmd != CommandEnum.NOT_LOGGED_IN_EVENT:
            # reset login attempts on any valid command
            self._login_attempts = 0

    async def request_status(self) -> bool:
        """Request current EVSE status (async)."""
        if not self._logged_in:
            raise NotLoggedInError("Please login before requesting status")
        await self.send_packet(self._build_packet(CommandEnum.CURRENT_STATUS_EVENT))
        return True

    async def request_essentials(self) -> bool:
        """Send some commands to get basic info."""
        if not self._logged_in:
            raise NotLoggedInError("Please login before getting essentials")
        await self.send_packet(self._build_packet(CommandEnum.NICKNAME_REQUEST, bytes([CommandEnum.GET_ACTION])))
        await self.send_packet(self._build_packet(CommandEnum.OUTPUT_AMPERAGE_REQUEST, bytes([CommandEnum.GET_ACTION])))
        await self.send_packet(self._build_packet(CommandEnum.CURRENT_STATUS_EVENT))
        return True

    async def start_charging(
        self,
        max_amps: int | None = None,
        start_date: datetime | None = None,
        duration_minutes: int | None = None,
    ) -> bool:
        """Send start charging request.

        max_amps: limit current
        start_date: schedule start (now if None)
        duration_minutes: max duration (65535 = unlimited)
        """
        if not self._logged_in:
            raise NotLoggedInError("Please login before starting charge")

        if self._status and self._status.current_state == CurrentStateEnum.CHARGING_RESERVATION:
            log.warning("Start charge send while a reservation is active, cancelling reservation first")
            await self.stop_charging()

        # handle defaults like this because you can force none otherwise
        if not start_date:
            start_date = datetime.now()
        if not duration_minutes or duration_minutes < 1 or duration_minutes > 65535:
            duration_minutes = 65535
        if not max_amps:
            max_amps = self._device_info.configured_max_amps or self._device_info.max_amps
        elif max_amps < 6 or max_amps > self._device_info.max_amps:
            raise ValueError(f"max_amps must be between 6 and {self._device_info.max_amps}")

        extra_payload = bytearray(47)

        # Line ID (seems to be always one 1, are there any devices with multiple lines?)
        struct.pack_into(">B", extra_payload, 0, 1)
        # User ID (16 bytes, ASCII encoded)
        struct.pack_into(">16s", extra_payload, 1, self.user_id.encode("ascii")[:16])
        # Charge ID (16 bytes, ASCII encoded)
        struct.pack_into(">16s", extra_payload, 17, start_date.strftime("%Y%m%d%H%M").encode("ascii")[:16])
        # Reservation: 0 for now, 1 if future reservation
        struct.pack_into(">B", extra_payload, 33, 0 if datetime.now() > start_date else 1)
        # Reservation date (current time in Shanghai epoch)
        struct.pack_into(">I", extra_payload, 34, self._datetime_to_shanghai_epoch(start_date))
        # Start type (always 1)
        struct.pack_into(">B", extra_payload, 38, 1)
        # Charge type (always 1)
        struct.pack_into(">B", extra_payload, 39, 1)
        # Max duration (65535 = highest possible, unlimited)
        struct.pack_into(">H", extra_payload, 40, duration_minutes)
        # Max energy (65535 = highest possible, unlimited)
        struct.pack_into(">H", extra_payload, 42, 65535)
        # Charge param 3 (always 65535)
        struct.pack_into(">H", extra_payload, 44, 65535)
        # Max electricity in amps
        struct.pack_into(">B", extra_payload, 46, max_amps)

        packet = self._build_packet(CommandEnum.CHARGE_START_REQUEST, extra_payload)
        await self.send_packet(packet)
        return True

    async def stop_charging(self) -> bool:
        """Send stop charging request."""
        if not self._logged_in:
            raise NotLoggedInError("Please login before stopping charge")

        extra_payload = bytes([1])  # port id
        await self.send_packet(self._build_packet(CommandEnum.CHARGE_STOP_REQUEST, extra_payload))
        return True

    def _datetime_to_shanghai_epoch(self, dt: datetime) -> int:
        """
        The EVSE handles time weirdly, time interpretation is always in Asia/Shanghai timezone.
        So any time object you give it it will convert to Asia/Shanghai.
        So we always convert to Asia/Shanghai here.
        """
        shanghai_tz = zoneinfo.ZoneInfo("Asia/Shanghai")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=shanghai_tz)
        else:
            dt = dt.astimezone(shanghai_tz)
        return int(dt.timestamp())

    def _shanghai_epoch_to_datetime(self, epoch: int) -> datetime:
        """
        Similarly we receive the time as epoch seconds but always in Asia/Shanghai timezone.
        """
        shanghai_tz = zoneinfo.ZoneInfo("Asia/Shanghai")
        dt = datetime.fromtimestamp(epoch).replace(tzinfo=shanghai_tz)
        return dt

    async def set_nickname(self, nickname: str) -> bool:
        """Set the EVSE nickname."""
        if not self._logged_in:
            raise NotLoggedInError("Please login before setting nickname")

        max_display_nickname = 28
        if len(nickname) > max_display_nickname:
            log.warning(f"Nickname too long, truncating to {max_display_nickname} characters")
            nickname = nickname[:max_display_nickname]

        # Prepend "ACP#" prefix as required by the protocol
        full_nickname = "ACP#" + nickname

        extra_payload = bytearray(33)
        extra_payload[0] = CommandEnum.SET_ACTION

        # Action: 0 = set, 1 = get
        struct.pack_into(">B", extra_payload, 0, CommandEnum.SET_ACTION)
        # Nickname (up to 20 bytes, ASCII encoded)
        struct.pack_into(f">{len(full_nickname)}s", extra_payload, 1, full_nickname.encode("ascii"))

        packet = self._build_packet(CommandEnum.NICKNAME_REQUEST, extra_payload)
        await self.send_packet(packet)
        return True

    async def set_output_amperage(self, amperage: int) -> bool:
        """Set the EVSE output amperage limit."""
        if not self._logged_in:
            raise NotLoggedInError("Please login before setting output amperage")
        if amperage < 6 or amperage > 32:
            raise ValueError("Amperage must be between 6 and 32")
        if self._device_info and amperage > self._device_info.max_amps:
            raise ValueError(f"Amperage exceeds device max of {self._device_info.max_amps}")
        extra_payload = bytearray(3)
        struct.pack_into(">B", extra_payload, 0, CommandEnum.SET_ACTION)
        struct.pack_into(">B", extra_payload, 1, amperage)

        packet = self._build_packet(CommandEnum.OUTPUT_AMPERAGE_REQUEST, extra_payload)
        await self.send_packet(packet)
        return True

    def _build_packet(self, cmd: CommandEnum, payload: bytes = b"") -> bytes:
        """Generic method to build a packet with given command and payload."""
        packet = bytearray(25 + len(payload))

        # Header
        struct.pack_into(">H", packet, 0, CommandEnum.HEADER)
        # Length
        struct.pack_into(">H", packet, 2, len(packet))
        # Key type
        packet[4] = 0x00
        # Device serial (8 bytes) - use device serial if available
        if self._device_info and self._device_info.serial_number:
            try:
                # Convert hex string to bytes
                serial_bytes = bytes.fromhex(self._device_info.serial_number)
                packet[5 : 5 + len(serial_bytes)] = serial_bytes
            except ValueError:
                # If serial is not valid hex, leave as zeros
                pass
        # Password (6 bytes)
        if self.password:
            password_bytes = self.password.encode("ascii")[:6]
        packet[13 : 13 + len(password_bytes)] = password_bytes
        # Command
        struct.pack_into(">H", packet, 19, cmd)
        # Payload
        packet[21 : 21 + len(payload)] = payload
        # Checksum
        checksum = sum(packet[:-4]) % 0xFFFF
        struct.pack_into(">H", packet, len(packet) - 4, checksum)
        # Tail
        struct.pack_into(">H", packet, len(packet) - 2, CommandEnum.TAIL)
        log.debug(f"Built packet: cmd={cmd.name}, len={len(packet)}, payload_len={len(payload)}")
        return bytes(packet)

    def _parse_device_info(self, data: DataPacket):
        """Parse device information from login response."""
        try:
            if data.length() < 25:
                return

            self._device_info = EvseDeviceInfo(
                type=data.get_int(0, 1),
                brand=data.get_string(1, 16),
                model=data.get_string(17, 16),
                hardware_version=data.get_string(33, 16),
                max_power=data.get_int(49, 4),
                max_amps=data.get_int(53, 1),
                serial_number=data.device_serial,
            )

        except Exception as err:
            log.error("Failed to parse device info: %s", err)

    def _parse_status_response(self, data: DataPacket):
        """Parse status response."""
        try:
            if data.length() < 33:
                return

            self._status = EvseStatus(
                line_id=data.get_int(0, 1),
                l1_voltage=data.get_int(1, 2) / 10,
                l1_amps=data.get_int(3, 2) / 100,
                current_power=data.get_int(5, 4),
                total_kwh=data.get_int(9, 4) / 100,
                inner_temperature=data.read_temperature(13),
                outer_temperature=data.read_temperature(15),
                emergency_stop=data.get_int(17, 1),
                plug_state=data.get_int(18, 1),
                output_state=data.get_int(19, 1),
                current_state=data.get_int(20, 1),
                errors=data.get_int(21, 4),
                l2_voltage=data.get_int(25, 2) / 10,
                l2_amps=data.get_int(27, 2) / 100,
                l3_voltage=data.get_int(29, 2) / 10,
                l3_amps=data.get_int(31, 2) / 100,
            )

            return self._status

        except Exception as err:
            log.error("Failed to parse status response: %s", err)

    def _parse_current_charging_status(self, data: DataPacket):
        """Parse AC charging status response."""
        try:
            if data.length() < 25:
                return

            self._charging_status = ChargingStatus(
                line_id=data.get_int(0, 1),
                current_state=data.get_int(1, 1),
                charge_id=data.get_string(2, 16),
                start_type=data.get_int(18, 1),
                charge_type=data.get_int(19, 1),
                max_duration_minutes=None if data.get_int(20, 2) == 65535 else data.get_int(20, 2),
                max_energy_kwh=None if data.get_int(22, 2) == 65535 else data.get_int(22, 2) * 0.01,
                charge_param3=None if data.get_int(24, 2) == 65535 else data.get_int(24, 2) * 0.01,
                reservation_datetime=self._shanghai_epoch_to_datetime(data.get_int(26, 4)),
                user_id=data.get_string(30, 16),
                max_electricity=data.get_int(46, 1),
                set_datetime=self._shanghai_epoch_to_datetime(data.get_int(47, 4)),
                duration_seconds=data.get_int(51, 4),
                start_kwh_counter=data.get_int(55, 4) / 100,
                current_kwh_counter=data.get_int(59, 4) / 100,
                charge_kwh=data.get_int(63, 4) / 100,
                charge_price=data.get_int(67, 4) / 100,
                fee_type=data.get_int(71, 1),
                charge_fee=data.get_int(72, 2) / 100,
            )

            return self._charging_status

        except Exception as err:
            log.error("Failed to parse AC charging status: %s", err)

    def get_latest_device_info(self) -> Optional[EvseDeviceInfo]:
        """Get the latest device info."""
        return self._device_info

    def get_latest_status(self) -> Optional[EvseStatus]:
        """Get the latest EVSE status."""
        return self._status

    def get_latest_charging_status(self) -> Optional[ChargingStatus]:
        """Get the latest charging status."""
        return self._charging_status

    @property
    def is_logged_in(self) -> bool:
        """Check if logged in."""
        return self._logged_in
