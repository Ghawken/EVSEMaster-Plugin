# EVSEMaster Indigo Plugin - simplified session manager, richer human-readable states
# - Uses event_callback (library signature)
# - Connect once, login opportunistically; rely on push + modest periodic refresh
# - Avoids duplicate managers (only scheduled from deviceStartComm)
# - Adds readable, English summary states (phase summary, power kW, safety status, charge limits, etc.)
try:
    import indigo
except ImportError:
    pass

import asyncio
import threading
import time
from datetime import datetime, timedelta

# 3rd-party EVSE library
try:
    from evsemaster.evse_protocol import SimpleEVSEProtocol
    from evsemaster.data_types import PlugStateEnum, CurrentStateEnum, NotLoggedInError
except Exception:
    SimpleEVSEProtocol = None
    PlugStateEnum = None
    CurrentStateEnum = None


STATUS_INTERVAL_SEC = 60
LOGIN_RETRY_MIN = 30
EVENT_GRACE_SEC = 90
RECONNECT_BACKOFF_MAX = 30.0


class Plugin(indigo.PluginBase):
    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)
        self.debug = True

        self._event_loop = None
        self._async_thread = None

        self._protocols = {}
        self._manager_tasks = {}
        self._periodic_tasks = {}

        self._transport_ready = {}
        self._authed_flag = {}
        self._last_event_at = {}
        self._last_login_attempt_at = {}

    # ========= Indigo lifecycle / thread bootstrap =========
    def startup(self):
        self.logger.debug("startup called")
        if SimpleEVSEProtocol is None:
            self.logger.error("evsemaster library not found. Ensure it's installed.")
            return

        self._event_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._run_async_thread, name="EVSEMasterAsync", daemon=True)
        self._async_thread.start()

    def shutdown(self):
        self.logger.debug("shutdown called")

    def _run_async_thread(self):
        self.logger.debug("_run_async_thread starting")
        assert self._event_loop is not None
        asyncio.set_event_loop(self._event_loop)
        self._event_loop.run_until_complete(self._async_stop())
        self._event_loop.close()

    async def _async_stop(self):
        while True:
            await asyncio.sleep(2.0)
            if self.stopThread:
                for dev_id, t in list(self._periodic_tasks.items()):
                    if t and not t.done():
                        t.cancel()
                for dev_id, t in list(self._manager_tasks.items()):
                    if t and not t.done():
                        t.cancel()
                for dev_id in list(self._protocols.keys()):
                    try:
                        await self._safe_disconnect(dev_id)
                    except Exception:
                        pass
                break

    # ========= Indigo device lifecycle =========
    def deviceStartComm(self, dev):
        if dev.deviceTypeId != "evseCharger":
            return
        self.logger.info(f"Starting EVSE device '{dev.name}'")
        dev.stateListOrDisplayStateIdChanged()
        try:
            dev.updateStateImageOnServer(indigo.kStateImageSel.Auto)
        except Exception:
            pass

        self._transport_ready[dev.id] = False
        self._authed_flag[dev.id] = False
        self._last_event_at[dev.id] = 0.0
        self._last_login_attempt_at[dev.id] = 0.0

        self._update_basic_states(dev.id, connected=False, status_text="Initializing...")
        self._ensure_manager(dev.id)

    def deviceStopComm(self, dev):
        if dev.deviceTypeId != "evseCharger":
            return
        self.logger.info(f"Stopping EVSE device '{dev.name}'")

        self._transport_ready[dev.id] = False
        self._authed_flag[dev.id] = False
        self._last_event_at[dev.id] = 0.0
        self._last_login_attempt_at[dev.id] = 0.0

        if self._event_loop:
            def _cancel_and_disconnect():
                pt = self._periodic_tasks.pop(dev.id, None)
                if pt and not pt.done():
                    pt.cancel()
                mt = self._manager_tasks.pop(dev.id, None)
                if mt and not mt.done():
                    mt.cancel()
                asyncio.create_task(self._safe_disconnect(dev.id))
            self._event_loop.call_soon_threadsafe(_cancel_and_disconnect)

    def validateDeviceConfigUi(self, values_dict, type_id, dev_id):
        errors = {}
        host = (values_dict.get("host") or "").strip()
        password = (values_dict.get("password") or "").strip()
        if not host:
            errors["host"] = "Please enter the EVSE IP/hostname."
        if not password or len(password) != 6 or not password.isdigit():
            errors["password"] = "PIN must be 6 digits."
        if errors:
            return (False, errors, values_dict)
        return (True, values_dict)

    # ========= Relay actions map to start/stop charging =========
    def actionControlDevice(self, action, dev):
        if dev.deviceTypeId != "evseCharger":
            return
        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            self.logger.info(f"Start charging requested for '{dev.name}'")
            self._schedule_coroutine(self._start_charging(dev.id))
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            self.logger.info(f"Stop charging requested for '{dev.name}'")
            self._schedule_coroutine(self._stop_charging(dev.id))
        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            charging = bool(dev.states.get("charging", False))
            if charging:
                self.logger.info(f"Toggling OFF (stop charging) for '{dev.name}'")
                self._schedule_coroutine(self._stop_charging(dev.id))
            else:
                self.logger.info(f"Toggling ON (start charging) for '{dev.name}'")
                self._schedule_coroutine(self._start_charging(dev.id))

    # ========= Internal orchestration =========
    def _ensure_manager(self, dev_id: int):
        if not self._event_loop:
            return
        if dev_id in self._manager_tasks and not self._manager_tasks[dev_id].done():
            return
        task = self._event_loop.create_task(self._device_session_manager(dev_id))
        self._manager_tasks[dev_id] = task

    def _schedule_coroutine(self, coro):
        if not self._event_loop:
            self.logger.error("Async loop not running.")
            return
        self._event_loop.call_soon_threadsafe(asyncio.create_task, coro)

    def start_charging_action(self, action, dev):
        """
        Action: Start Charging
        """
        self.logger.info(f"Action: Start Charging '{dev.name}'")
        self._schedule_coroutine(self._start_charging(dev.id))

    def stop_charging_action(self, action, dev):
        """
        Action: Stop Charging
        """
        self.logger.info(f"Action: Stop Charging '{dev.name}'")
        self._schedule_coroutine(self._stop_charging(dev.id))

    def set_output_amps_action(self, action, dev):
        """
        Action: Set Output Amperage
        """
        raw = (action.props.get("amps") or "").strip()
        try:
            amps = int(raw)
        except Exception:
            self.logger.error(f"Set Output Amperage: invalid amperage '{raw}' for '{dev.name}'")
            return
        if amps < 6 or amps > 32:
            self.logger.error(f"Set Output Amperage: {amps}A out of range (6–32) for '{dev.name}'")
            return
        self.logger.info(f"Action: Set Output Amperage to {amps}A for '{dev.name}'")
        self._schedule_coroutine(self._set_output_amperage(dev.id, amps))

    async def _set_output_amperage(self, dev_id: int, amps: int):
        """
        Send output amperage limit to EVSE (library enforces device max).
        """
        proto = self._protocols.get(dev_id)
        dev = indigo.devices.get(dev_id)
        if not proto or not dev:
            return
        try:
            # Ensure authenticated if possible
            if hasattr(proto, "is_logged_in") and not proto.is_logged_in:
                if hasattr(self, "_attempt_login"):
                    await self._attempt_login(dev_id, proto, why="set-output-amps")
                else:
                    await proto.login()

            await proto.set_output_amperage(amps)
            self.logger.info(f"Set output amperage {amps}A sent to '{dev.name}'")

            # Optionally reflect immediately; the EVSE will also send an event
            try:
                dev.updateStateOnServer("di_configured_max_amps", int(amps))
            except Exception:
                pass

        except Exception as ex:
            try:
                dev.updateStateOnServer("last_error", str(ex))
            except Exception:
                pass
            self.logger.error(f"Set output amperage failed for '{dev.name}': {ex}")

    async def _safe_disconnect(self, dev_id: int):
        proto = self._protocols.get(dev_id)
        if proto:
            try:
                await proto.disconnect()
            except Exception:
                pass
        self._protocols.pop(dev_id, None)

    async def _device_session_manager(self, dev_id: int):
        backoff = 2.0
        while not self.stopThread:
            dev = indigo.devices.get(dev_id)
            if not dev or not dev.enabled or not dev.configured:
                await asyncio.sleep(2.0)
                continue

            host = (dev.pluginProps.get("host") or "").strip()
            password = (dev.pluginProps.get("password") or "").strip()
            if not host or not password:
                self._update_basic_states(dev_id, connected=False, status_text="Missing host or PIN")
                await asyncio.sleep(5.0)
                continue

            def event_callback(event, data):
                # Any received event indicates transport connectivity
                self._last_event_at[dev_id] = time.monotonic()
                try:
                    payload = data.model_dump() if hasattr(data, "model_dump") else data
                except Exception:
                    payload = data
                self.logger.debug(f"Event received: {event}: {payload}")
                self._update_connected_flag(dev_id, True)
                self._handle_event(dev_id, event, data)

            try:
                proto = SimpleEVSEProtocol(host=host, password=password, event_callback=event_callback)
                self._protocols[dev_id] = proto

                # Connect transport
                self._update_basic_states(dev_id, connected=False, status_text="Connecting...")
                ok = await proto.connect()
                if not ok:
                    self._update_basic_states(dev_id, connected=False, status_text="Connect failed")
                    raise RuntimeError("Connect failed")
                self._update_connected_flag(dev_id, True)
                self.logger.debug(f"Transport ready for '{dev.name}' (listen {proto.listen_port} -> send {proto.send_port})")

                # Initial login (non-fatal if it fails; we can still receive status events)
                await self._attempt_login(dev_id, proto, why="initial")

                # Start a single periodic status task
                if dev_id in self._periodic_tasks and not self._periodic_tasks[dev_id].done():
                    self._periodic_tasks[dev_id].cancel()
                self._periodic_tasks[dev_id] = asyncio.create_task(self._periodic_status_task(dev_id))

                # Keep session alive; opportunistic re-login if quiet
                while not self.stopThread and self._protocols.get(dev_id) is proto:
                    self._update_connected_flag(dev_id, True)
                    self._update_auth_flag(dev_id, proto.is_logged_in)

                    if not proto.is_logged_in:
                        quiet = (time.monotonic() - self._last_event_at.get(dev_id, 0.0)) > EVENT_GRACE_SEC
                        since_last_try = time.monotonic() - self._last_login_attempt_at.get(dev_id, 0.0)
                        if quiet and since_last_try >= LOGIN_RETRY_MIN:
                            await self._attempt_login(dev_id, proto, why="retry-quiet")
                    await asyncio.sleep(2.0)

            except asyncio.CancelledError:
                break
            except Exception as ex:
                name = dev.name if dev else f"id {dev_id}"
                self.logger.error(f"Connection error for '{name}': {ex}")
                self._update_basic_states(dev_id, connected=False, status_text=f"Error: {ex}")
            finally:
                pt = self._periodic_tasks.pop(dev_id, None)
                if pt and not pt.done():
                    pt.cancel()
                await self._safe_disconnect(dev_id)
                self._update_connected_flag(dev_id, False)
                self._update_auth_flag(dev_id, False)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, RECONNECT_BACKOFF_MAX)

    async def _attempt_login(self, dev_id: int, proto: SimpleEVSEProtocol, why: str):
        dev = indigo.devices.get(dev_id)
        if not dev:
            return
        now_m = time.monotonic()
        if why != "initial" and now_m - self._last_login_attempt_at.get(dev_id, 0.0) < LOGIN_RETRY_MIN:
            return
        self._last_login_attempt_at[dev_id] = now_m

        self.logger.debug(f"Attempting login for '{dev.name}' ({why})")
        self._update_basic_states(dev_id, connected=True, status_text="Logging in...")
        ok = False
        try:
            ok = await proto.login()
        except Exception as ex:
            self.logger.debug(f"Login exception for '{dev.name}': {ex}")

        if ok:
            self._update_auth_flag(dev_id, True)
            self._update_basic_states(dev_id, connected=True, status_text="Connected")
            self.logger.info(f"Connected to EVSE '{dev.name}' ({(dev.pluginProps.get('host') or '').strip()})")
        else:
            self._update_auth_flag(dev_id, False)
            # Stay passive; do not tear down transport
            self._update_basic_states(dev_id, connected=True, status_text="Passive mode (login failed)")
            self.logger.debug(f"Login failed for '{dev.name}', staying passive and listening for events")

    async def _periodic_status_task(self, dev_id: int):
        try:
            while not self.stopThread:
                proto = self._protocols.get(dev_id)
                dev = indigo.devices.get(dev_id)
                if not proto or not dev:
                    return
                try:
                    if proto.is_logged_in:
                        self.logger.debug(f"Periodic: requesting status for '{dev.name}'")
                        await proto.request_status()
                    else:
                        quiet = (time.monotonic() - self._last_event_at.get(dev_id, 0.0)) > EVENT_GRACE_SEC
                        since_last_try = time.monotonic() - self._last_login_attempt_at.get(dev_id, 0.0)
                        if quiet and since_last_try >= LOGIN_RETRY_MIN:
                            await self._attempt_login(dev_id, proto, why="periodic")
                except NotLoggedInError:
                    self.logger.debug(f"Periodic: NotLoggedInError for '{dev.name}'")
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    self._update_basic_states(dev_id, connected=True, status_text=f"Poll error: {ex}")
                await asyncio.sleep(STATUS_INTERVAL_SEC)
        except asyncio.CancelledError:
            return

    # ========= Flags / device states =========
    def _update_connected_flag(self, dev_id: int, connected: bool):
        prev = self._transport_ready.get(dev_id, False)
        self._transport_ready[dev_id] = bool(connected)
        if prev != connected:
            self._update_basic_states(
                dev_id, connected=connected, status_text=("Connected" if connected else "Disconnected")
            )

    def _update_auth_flag(self, dev_id: int, authed: bool):
        prev = self._authed_flag.get(dev_id, False)
        self._authed_flag[dev_id] = bool(authed)
        dev = indigo.devices.get(dev_id)
        if dev:
            try:
                dev.updateStateOnServer("auth_ok", bool(authed))
            except Exception:
                pass
        if prev != authed and dev:
            self.logger.debug(f"Auth state for '{dev.name}': {'authenticated' if authed else 'unauthenticated'}")

    def _update_basic_states(self, dev_id: int, connected: bool, status_text: str):
        dev = indigo.devices.get(dev_id)
        if not dev:
            return
        kv = [
            {"key": "connected", "value": bool(connected)},
            {"key": "status_text", "value": status_text},
            {"key": "last_update", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        ]
        try:
            dev.updateStatesOnServer(kv)
        except Exception:
            pass

    # ========= Event handling / state updates (with readable states) =========
    def _handle_event(self, dev_id: int, event: str, data):
        dev = indigo.devices.get(dev_id)
        if not dev:
            return

        # Convert Pydantic models to dict safely
        as_dict = None
        if hasattr(data, "model_dump"):
            try:
                as_dict = data.model_dump()
            except Exception:
                as_dict = None
        if as_dict is None and isinstance(data, dict):
            as_dict = data
        if as_dict is None:
            return

        kv = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        kv.append({"key": "last_update", "value": now_str})

        # Helpers
        def add_num(key, val, places=None):
            if val is None or isinstance(val, bool):
                return
            try:
                f = float(val)
            except Exception:
                return
            item = {"key": key, "value": f}
            if places is not None:
                item["uiValue"] = f"{f:.{places}f}"
                item["decimalPlaces"] = places
            kv.append(item)

        def add_int(key, val):
            if val is None:
                return
            try:
                kv.append({"key": key, "value": int(val)})
            except Exception:
                pass

        def add_bool(key, val):
            if val is None:
                return
            kv.append({"key": key, "value": bool(val)})

        def add_str(key, val):
            if val is None:
                return
            kv.append({"key": key, "value": str(val)})

        if event == "EvseDeviceInfo":
            add_int("di_type", as_dict.get("type"))
            add_str("di_brand", as_dict.get("brand"))
            add_str("di_model", as_dict.get("model"))
            add_str("di_hardware_version", as_dict.get("hardware_version"))
            add_int("di_max_power_w", as_dict.get("max_power"))
            # kW convenience
            try:
                mpw = as_dict.get("max_power")
                if mpw is not None:
                    add_num("di_max_power_kw", float(mpw) / 1000.0, 3)
            except Exception:
                pass
            add_int("di_max_amps", as_dict.get("max_amps"))
            add_str("di_serial_number", as_dict.get("serial_number"))
            add_str("di_nickname", as_dict.get("nickname"))
            add_int("di_configured_max_amps", as_dict.get("configured_max_amps"))
            add_str("status_text", "Device info updated")

        elif event == "EvseStatus":
            add_int("st_line_id", as_dict.get("line_id"))
            add_num("st_inner_temp_c", as_dict.get("inner_temperature"), 1)
            add_num("st_outer_temp_c", as_dict.get("outer_temperature"), 1)
            add_bool("st_emergency_stop", as_dict.get("emergency_stop"))
            add_str("safety_status", "EMERGENCY STOP ACTIVE" if as_dict.get("emergency_stop") else "OK")

            # Enums may arrive as Enum instances; normalize
            plug_val = as_dict.get("plug_state")
            cur_val = as_dict.get("current_state")
            plug_raw = getattr(plug_val, "value", plug_val)
            cur_raw = getattr(cur_val, "value", cur_val)

            add_int("st_plug_state", plug_raw)
            add_str("st_plug_state_name", self._enum_name(PlugStateEnum, plug_raw))
            add_int("st_output_state", as_dict.get("output_state"))
            add_str("st_output_state_name", self._output_state_name(as_dict.get("output_state")))
            add_int("st_current_state", cur_raw)
            add_str("st_current_state_name", self._enum_name(CurrentStateEnum, cur_raw))
            add_int("st_errors", as_dict.get("errors"))

            # Per-phase
            add_num("st_l1_voltage_v", as_dict.get("l1_voltage"), 1)
            add_num("st_l1_amps_a", as_dict.get("l1_amps"), 2)
            add_num("st_l2_voltage_v", as_dict.get("l2_voltage"), 1)
            add_num("st_l2_amps_a", as_dict.get("l2_amps"), 2)
            add_num("st_l3_voltage_v", as_dict.get("l3_voltage"), 1)
            add_num("st_l3_amps_a", as_dict.get("l3_amps"), 2)
            # Phase summary (readable)
            try:
                l1v, l1a = as_dict.get("l1_voltage"), as_dict.get("l1_amps")
                l2v, l2a = as_dict.get("l2_voltage"), as_dict.get("l2_amps")
                l3v, l3a = as_dict.get("l3_voltage"), as_dict.get("l3_amps")
                if None not in (l1v, l1a, l2v, l2a, l3v, l3a):
                    add_str(
                        "phase_summary",
                        f"L1 {l1v:.1f}V {l1a:.2f}A | L2 {l2v:.1f}V {l2a:.2f}A | L3 {l3v:.1f}V {l3a:.2f}A",
                    )
            except Exception:
                pass

            add_int("st_current_power_w", as_dict.get("current_power"))
            try:
                p = as_dict.get("current_power")
                if p is not None:
                    add_num("st_current_power_kw", float(p) / 1000.0, 3)
            except Exception:
                pass
            add_num("st_total_kwh", as_dict.get("total_kwh"), 2)

            # Derived flags
            vehicle_connected = None
            try:
                if plug_raw is not None and PlugStateEnum is not None:
                    vehicle_connected = int(plug_raw) != int(PlugStateEnum.DISCONNECTED)
            except Exception:
                pass
            charging = None
            try:
                if cur_raw is not None and CurrentStateEnum is not None:
                    charging = int(cur_raw) == int(CurrentStateEnum.CHARGING)
            except Exception:
                pass
            if vehicle_connected is not None:
                add_bool("vehicle_connected", vehicle_connected)
            if charging is not None:
                add_bool("charging", charging)
                kv.append({"key": "onOffState", "value": bool(charging)})

            # Human summary
            add_str("state_summary", f"{self._enum_name(CurrentStateEnum, cur_raw)} | Plug: {self._enum_name(PlugStateEnum, plug_raw)}")

        elif event == "ChargingStatus":
            add_int("ch_line_id", as_dict.get("line_id"))
            cur_raw = getattr(as_dict.get("current_state"), "value", as_dict.get("current_state"))
            add_int("ch_current_state", cur_raw)
            add_str("ch_current_state_name", self._enum_name(CurrentStateEnum, cur_raw))
            add_str("ch_charge_id", as_dict.get("charge_id"))
            add_int("ch_start_type", as_dict.get("start_type"))
            add_str("ch_start_type_name", self._start_type_name(as_dict.get("start_type")))
            add_int("ch_charge_type", as_dict.get("charge_type"))
            add_str("ch_charge_type_name", self._charge_type_name(as_dict.get("charge_type")))
            add_int("ch_max_duration_min", as_dict.get("max_duration_minutes"))
            add_num("ch_max_energy_kwh", as_dict.get("max_energy_kwh"), 2)
            add_num("ch_charge_param3", as_dict.get("charge_param3"), 2)
            # datetimes -> ISO-like short strings
            rd = as_dict.get("reservation_datetime")
            sd = as_dict.get("set_datetime")
            add_str("ch_reservation_datetime", str(rd) if rd is not None else "")
            add_str("ch_reservation_time", self._fmt_datetime(rd) if rd else "")
            add_str("ch_user_id", as_dict.get("user_id"))
            add_int("ch_max_electricity_a", as_dict.get("max_electricity"))
            add_str("ch_set_datetime", str(sd) if sd is not None else "")
            add_int("ch_duration_seconds", as_dict.get("duration_seconds"))
            add_str("ch_session_duration", self._fmt_duration(as_dict.get("duration_seconds")))
            add_num("ch_start_kwh_counter", as_dict.get("start_kwh_counter"), 2)
            add_num("ch_current_kwh_counter", as_dict.get("current_kwh_counter"), 2)
            add_num("ch_charge_kwh", as_dict.get("charge_kwh"), 2)
            add_num("ch_charge_price", as_dict.get("charge_price"), 2)
            add_int("ch_fee_type", as_dict.get("fee_type"))
            add_str("ch_fee_type_name", self._fee_type_name(as_dict.get("fee_type")))
            add_num("ch_charge_fee", as_dict.get("charge_fee"), 2)
            # Summary limits
            try:
                max_a = as_dict.get("max_electricity")
                max_min = as_dict.get("max_duration_minutes")
                max_kwh = as_dict.get("max_energy_kwh")
                limits = []
                if max_a is not None:
                    limits.append(f"{max_a}A")
                if max_min is not None:
                    limits.append(f"{max_min} min")
                if max_kwh is not None:
                    limits.append(f"{max_kwh:.2f} kWh")
                add_str("ch_charge_limits", ", ".join(limits))
            except Exception:
                pass
            add_str("status_text", "Charging status updated")

        # Push updates
        try:
            if kv:
                dev.updateStatesOnServer(kv)
        except Exception as ex:
            self.logger.error(f"Failed to update states for '{dev.name}': {ex}")

    # ========= Formatting helpers =========
    def _enum_name(self, enum_cls, value):
        try:
            if enum_cls is None or value is None:
                return ""
            return enum_cls(int(value)).name.replace("_", " ").title()
        except Exception:
            return str(value) if value is not None else ""

    def _output_state_name(self, raw):
        # Heuristic/guess based on observed values; adjust if spec known
        try:
            v = int(raw) if raw is not None else None
        except Exception:
            v = None
        mapping = {
            0: "Disabled",
            1: "Idle",
            2: "Enabled",
        }
        return mapping.get(v, str(raw) if raw is not None else "")

    def _start_type_name(self, raw):
        # Unknown mapping; present value or simple buckets
        try:
            v = int(raw)
        except Exception:
            return "" if raw is None else str(raw)
        return {
            0: "Unknown",
            1: "Immediate",
            5: "Key/Authorization",
        }.get(v, f"Type {v}")

    def _charge_type_name(self, raw):
        try:
            v = int(raw)
        except Exception:
            return "" if raw is None else str(raw)
        return {
            0: "Unknown",
            1: "AC",
            2: "DC",
        }.get(v, f"Type {v}")

    def _fee_type_name(self, raw):
        try:
            v = int(raw)
        except Exception:
            return "" if raw is None else str(raw)
        return {
            0: "None",
            1: "Flat",
            2: "Time-Based",
            3: "Energy-Based",
        }.get(v, f"Type {v}")

    def _fmt_duration(self, seconds):
        try:
            s = int(seconds or 0)
        except Exception:
            return ""
        h = s // 3600
        m = (s % 3600) // 60
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"

    def _fmt_datetime(self, dt):
        try:
            if not dt:
                return ""
            # Render short, readable local-like text (dt may be tz-aware)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(dt) if dt is not None else ""

    # ========= Async control helpers =========
    async def _start_charging(self, dev_id: int):
        proto = self._protocols.get(dev_id)
        dev = indigo.devices.get(dev_id)
        if not proto or not dev:
            return
        try:
            if not proto.is_logged_in:
                self.logger.debug(f"Start charge: attempting login for '{dev.name}'")
                await self._attempt_login(dev_id, proto, why="user-start")
            await proto.start_charging()
            self.logger.info(f"Start charging sent to '{dev.name}'")
            dev.updateStateOnServer("onOffState", True)
        except Exception as ex:
            dev.updateStateOnServer("last_error", str(ex))
            self.logger.error(f"Start charging failed for '{dev.name}': {ex}")

    async def _stop_charging(self, dev_id: int):
        proto = self._protocols.get(dev_id)
        dev = indigo.devices.get(dev_id)
        if not proto or not dev:
            return
        try:
            if not proto.is_logged_in:
                self.logger.debug(f"Stop charge: attempting login for '{dev.name}'")
                await self._attempt_login(dev_id, proto, why="user-stop")
            await proto.stop_charging()
            self.logger.info(f"Stop charging sent to '{dev.name}'")
            dev.updateStateOnServer("onOffState", False)
        except Exception as ex:
            dev.updateStateOnServer("last_error", str(ex))
            self.logger.error(f"Stop charging failed for '{dev.name}': {ex}")