"""Run inside the HA container: python -m unittest discover -s tests -p test_hvac_presets.py.

All MQTT transport is mocked; these tests never operate an appliance.
"""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import homeassistant  # noqa: F401  # Initialize HA's schema compatibility first.
import voluptuous as vol
from homeassistant.core import State
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.exceptions import ServiceValidationError

from custom_components.tasmota_irhvac import climate
from custom_components.tasmota_irhvac.presets import PRESET_FROST, FROST_SCHEMA

CONFIGS = {
    "gree": {
        "name": "Gree heat pump",
        "vendor": "GREE",
        "hvac_model": "YAW1F",
        "command_topic": "cmnd/gree_ac/irhvac",
        "state_topic": "tele/gree_ac/RESULT",
        "min_temp": 16,
        "max_temp": 30,
        "target_temp": 20,
        "supported_modes": ["heat", "cool", "fan_only", "off"],
        "supported_fan_speeds": ["medium", "high", "auto"],
        "frost_protection": {
            "command": "econo",
            "temperature": 8,
            "comfort_temperature": 20,
        },
    },
    "fujitsu": {
        "name": "Fujitsu heat pump",
        "vendor": "FUJITSU_AC",
        "hvac_model": "ARRAH2E",
        "command_topic": "cmnd/fujitsu_ac/irhvac",
        "state_topic": "tele/fujitsu_ac/RESULT",
        "min_temp": 16,
        "max_temp": 30,
        "target_temp": 20,
        "supported_modes": ["heat", "cool", "fan_only", "off"],
        "supported_fan_speeds": ["medium", "high", "auto"],
        "frost_protection": {
            "command": "irsend",
            "temperature": 10,
            "comfort_temperature": 20,
            "topic": "cmnd/fujitsu_ac/irsend",
            "payload": {
                "Protocol": "FUJITSU_AC",
                "Bits": 128,
                "Data": "0x1463001010FE0930410B001806122034",
            },
            "match_state": {"Power": "On", "Mode": "Fan", "Temp": 10},
        },
    },
    "legacy": {
        "name": "Legacy heat pump",
        "vendor": "FUJITSU_AC",
        "command_topic": "cmnd/legacy_ac/irhvac",
        "state_topic": "tele/legacy_ac/RESULT",
        "away_temp": 10,
        "target_temp": 20,
        "min_temp": 10,
    },
}


class PresetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = patch.object(
            climate.mqtt, "async_publish", new_callable=AsyncMock
        )
        self.publish = self.transport.start()
        self.addCleanup(self.transport.stop)

    def entity(self, filename="gree", **overrides):
        values = deepcopy(CONFIGS[filename])
        values["platform"] = "tasmota_irhvac"
        values.update(overrides)
        config = climate.PLATFORM_SCHEMA(values)
        hass = SimpleNamespace(
            config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°C")),
            states=SimpleNamespace(get=lambda _: None),
        )
        e = climate.TasmotaIrhvac(hass, config["vendor"], config)
        e.async_schedule_update_ha_state = lambda: None
        e.async_write_ha_state = lambda: None
        return e

    def payload(self):
        return json.loads(self.publish.call_args.args[2])

    async def receiver(self, e):
        subscriptions = {}

        async def subscribe(hass, topic, handler):
            subscriptions[topic] = handler
            return lambda: None

        with patch.object(climate.mqtt, "async_subscribe", side_effect=subscribe):
            await e._subscribe_topics()

        async def receive(data):
            await subscriptions[e.state_topic](
                SimpleNamespace(payload=json.dumps(data))
            )

        return receive

    async def test_shared_presets_are_opt_in(self):
        for filename in ("gree", "fujitsu"):
            self.assertEqual(
                self.entity(filename).preset_modes, ["none", PRESET_FROST, "comfort"]
            )
        self.assertEqual(self.entity("legacy").preset_modes, ["none", "away"])

    async def test_gree_frost_uses_econo_and_valid_encoded_temperature(self):
        e = self.entity()
        await e.async_set_preset_mode(PRESET_FROST)
        self.assertEqual(
            (e.hvac_mode, e.target_temperature, e.preset_mode),
            ("heat", 8, PRESET_FROST),
        )
        self.assertEqual(self.payload()["Econo"], "on")
        self.assertEqual(self.payload()["Mode"], "heat")
        self.assertEqual(self.payload()["Power"], "on")
        self.assertEqual(self.payload()["Temp"], 20)
        self.assertEqual(e._econo, "on")

    async def test_fujitsu_frost_preserves_known_raw_frame(self):
        e = self.entity("fujitsu")
        await e.async_set_preset_mode(PRESET_FROST)
        self.assertEqual(self.publish.call_args.args[1], "cmnd/fujitsu_ac/irsend")
        self.assertEqual(
            self.payload(),
            {
                "Protocol": "FUJITSU_AC",
                "Bits": 128,
                "Data": "0x1463001010FE0930410B001806122034",
            },
        )
        self.assertEqual(
            (e.hvac_mode, e.target_temperature, e.fan_mode), ("heat", 10, "auto")
        )

    async def test_comfort_exits_frost_and_heats_to_twenty(self):
        for filename in ("gree", "fujitsu"):
            e = self.entity(filename)
            await e.async_set_preset_mode(PRESET_FROST)
            await e.async_set_preset_mode("comfort")
            self.assertEqual(
                (e.hvac_mode, e.target_temperature, e.preset_mode),
                ("heat", 20, "comfort"),
            )
            self.assertEqual(self.publish.call_args.args[1], e.topic)
            self.assertEqual(self.payload()["Temp"], 20)
            self.assertEqual(self.payload()["Power"], "on")
            self.assertEqual(self.payload()["Econo"], "off")
            self.assertEqual(self.payload()["Clean"], "off")

    async def test_none_restores_previous_normal_target(self):
        for filename in ("gree", "fujitsu"):
            e = self.entity(filename)
            await e.async_set_temperature(temperature=23, hvac_mode="heat")
            await e.async_set_preset_mode(PRESET_FROST)
            await e.async_set_preset_mode("none")
            self.assertEqual((e.target_temperature, e.preset_mode), (23, "none"))
            self.assertEqual(self.payload()["Temp"], 23)

    async def test_dial_exits_frost(self):
        for filename in ("gree", "fujitsu"):
            e = self.entity(filename)
            await e.async_set_preset_mode(PRESET_FROST)
            await e.async_set_temperature(temperature=22)
            self.assertEqual((e.target_temperature, e.preset_mode), (22, "none"))
            self.assertEqual(self.payload()["Temp"], 22)
            self.assertEqual(self.payload()["Econo"], "off")

    async def test_cool_mode_exits_frost(self):
        for filename in ("gree", "fujitsu"):
            e = self.entity(filename)
            await e.async_set_preset_mode(PRESET_FROST)
            await e.async_set_hvac_mode("cool")
            self.assertEqual(
                (e.hvac_mode, e.target_temperature, e.preset_mode), ("cool", 20, "none")
            )
            self.assertEqual(self.payload()["Mode"], "cool")

    async def test_off_uses_power_off_not_raw_frost(self):
        for filename in ("gree", "fujitsu"):
            e = self.entity(filename)
            await e.async_set_preset_mode(PRESET_FROST)
            await e.async_turn_off()
            self.assertEqual((e.hvac_mode, e.preset_mode), ("off", "none"))
            self.assertEqual(self.payload()["Power"], "off")
            self.assertEqual(self.publish.call_args.args[1], e.topic)

    async def test_existing_econo_service_controls_frost(self):
        e = self.entity()
        await e.async_set_econo("on", "SendStore")
        self.assertEqual(e.preset_mode, PRESET_FROST)
        await e.async_set_econo("off", "SendStore")
        self.assertEqual(e.preset_mode, "none")
        self.assertEqual(self.payload()["Econo"], "off")

    async def test_manual_low_temperature_cannot_fake_frost(self):
        e = self.entity()
        with self.assertRaises(ServiceValidationError):
            await e.async_set_temperature(temperature=10)
        self.publish.assert_not_awaited()

    async def test_away_is_not_offered_and_rejected(self):
        e = self.entity()
        with self.assertRaises(ServiceValidationError):
            await e.async_set_preset_mode("away")
        self.assertEqual(e.target_temperature, 20)
        self.publish.assert_not_awaited()

    async def test_gree_feedback_retains_frost_display(self):
        e = self.entity()
        receive = await self.receiver(e)
        await receive(
            {
                "IRHVAC": {
                    "Vendor": "GREE",
                    "Power": "On",
                    "Mode": "Heat",
                    "Temp": 20,
                    "Econo": "On",
                }
            }
        )
        self.assertEqual((e.preset_mode, e.target_temperature), (PRESET_FROST, 8))
        self.assertEqual(e._econo, "on")
        self.publish.assert_not_awaited()

    async def test_fujitsu_feedback_normalizes_wire_fan_mode_to_heat(self):
        e = self.entity("fujitsu")
        receive = await self.receiver(e)
        await receive(
            {
                "IrReceived": {
                    "Protocol": "FUJITSU_AC",
                    "Bits": 128,
                    "Data": "0x1463001010FE0930410B001806122034",
                    "IRHVAC": {
                        "Vendor": "FUJITSU_AC",
                        "Power": "On",
                        "Mode": "Fan",
                        "Temp": 10,
                    },
                }
            }
        )
        self.assertEqual(
            (e.hvac_mode, e.preset_mode, e.target_temperature),
            ("heat", PRESET_FROST, 10),
        )
        self.publish.assert_not_awaited()

    async def test_fujitsu_decoded_feedback_recognizes_frost_without_raw_data(self):
        e = self.entity("fujitsu")
        receive = await self.receiver(e)
        await receive(
            {
                "IRHVAC": {
                    "Vendor": "FUJITSU_AC",
                    "Power": "On",
                    "Mode": "Fan",
                    "Temp": 10,
                }
            }
        )
        self.assertEqual((e.hvac_mode, e.preset_mode), ("heat", PRESET_FROST))

    async def test_remote_normal_heat_cancels_frost(self):
        for filename, vendor in (("gree", "GREE"), ("fujitsu", "FUJITSU_AC")):
            e = self.entity(filename)
            receive = await self.receiver(e)
            await e.async_set_preset_mode(PRESET_FROST)
            await receive(
                {
                    "IRHVAC": {
                        "Vendor": vendor,
                        "Power": "On",
                        "Mode": "Heat",
                        "Temp": 23,
                        "Econo": "Off",
                    }
                }
            )
            self.assertEqual((e.preset_mode, e.target_temperature), ("none", 23))

    async def test_remote_off_cancels_frost(self):
        e = self.entity("fujitsu")
        receive = await self.receiver(e)
        await e.async_set_preset_mode(PRESET_FROST)
        await receive({"IRHVAC": {"Vendor": "FUJITSU_AC", "Power": "Off"}})
        self.assertEqual((e.hvac_mode, e.preset_mode), ("off", "none"))

    async def test_partial_feedback_does_not_clear_frost(self):
        for filename, vendor in (("gree", "GREE"), ("fujitsu", "FUJITSU_AC")):
            e = self.entity(filename)
            receive = await self.receiver(e)
            await e.async_set_preset_mode(PRESET_FROST)
            await receive({"IRHVAC": {"Vendor": vendor, "FanSpeed": "Auto"}})
            self.assertEqual(e.preset_mode, PRESET_FROST)

    async def test_null_temperature_feedback_does_not_break_controls(self):
        e = self.entity()
        receive = await self.receiver(e)
        await receive({"IRHVAC": {"Vendor": "GREE", "Temp": None}})
        self.assertEqual(e.target_temperature, 20)

    async def test_legacy_away_still_restores_its_target(self):
        e = self.entity("legacy")
        await e.async_set_temperature(temperature=22, hvac_mode="heat")
        await e.async_set_preset_mode("away")
        self.assertEqual(e.target_temperature, 10)
        await e.async_set_preset_mode("none")
        self.assertEqual(e.target_temperature, 22)

    async def test_restart_restores_frost_without_ir_transmission(self):
        for filename in ("gree", "fujitsu"):
            e = self.entity(filename)
            e.async_get_last_state = AsyncMock(
                return_value=State(
                    "climate.test",
                    "heat",
                    {
                        "temperature": 8 if filename == "gree" else 10,
                        "preset_mode": PRESET_FROST,
                        "normal_target_temperature": 23,
                        "normal_fan_mode": "medium",
                        "last_on_mode": "heat",
                    },
                )
            )
            with (
                patch.object(
                    RestoreEntity, "async_added_to_hass", new_callable=AsyncMock
                ),
                patch.object(
                    climate.mqtt, "async_wait_for_mqtt_client", new_callable=AsyncMock
                ),
                patch.object(climate.mqtt, "async_subscribe", new_callable=AsyncMock),
                patch.object(climate.ha_event, "async_track_state_change_event"),
            ):
                await e.async_added_to_hass()
            self.assertEqual(e.preset_mode, PRESET_FROST)
            self.assertEqual(e._presets.normal_target, 23)
        self.publish.assert_not_awaited()

    async def test_restart_repairs_old_null_target_without_transmitting(self):
        e = self.entity()
        e._presets.restore(
            State("climate.test", "heat", {"temperature": None, "preset_mode": "away"})
        )
        self.assertEqual((e.target_temperature, e.preset_mode), (20, "none"))
        self.publish.assert_not_awaited()

    async def test_comfort_survives_feedback_but_manual_target_clears_it(self):
        e = self.entity()
        receive = await self.receiver(e)
        await e.async_set_preset_mode("comfort")
        await receive(
            {
                "IRHVAC": {
                    "Vendor": "GREE",
                    "Power": "On",
                    "Mode": "Heat",
                    "Temp": 20,
                    "Econo": "Off",
                }
            }
        )
        self.assertEqual(e.preset_mode, "comfort")
        await receive(
            {
                "IRHVAC": {
                    "Vendor": "GREE",
                    "Power": "On",
                    "Mode": "Heat",
                    "Temp": 22,
                    "Econo": "Off",
                }
            }
        )
        self.assertEqual(e.preset_mode, "none")

    async def test_upstream_fractional_setpoint_normalization_is_preserved(self):
        e = self.entity(temp_step=1)
        await e.async_set_temperature(temperature=22.5, hvac_mode="heat")
        self.assertEqual(e.target_temperature, 23)
        self.assertEqual(e._presets.normal_target, 23)
        await e.async_set_temperature(temperature=21.5)
        self.assertEqual(e.target_temperature, 21)

    async def test_frost_feedback_keeps_the_previous_normal_target(self):
        e = self.entity()
        receive = await self.receiver(e)
        await e.async_set_temperature(temperature=23, hvac_mode="heat")
        await receive(
            {
                "IRHVAC": {
                    "Vendor": "GREE",
                    "Power": "On",
                    "Mode": "Heat",
                    "Temp": 16,
                    "Econo": "On",
                }
            }
        )
        self.assertEqual(e._presets.normal_target, 23)
        await e.async_set_preset_mode("none")
        self.assertEqual(e.target_temperature, 23)

    async def test_raw_decoded_matching_is_explicit_and_optional(self):
        config = deepcopy(CONFIGS["fujitsu"]["frost_protection"])
        config.pop("match_state")
        e = self.entity("fujitsu", frost_protection=config)
        receive = await self.receiver(e)
        await receive(
            {
                "IRHVAC": {
                    "Vendor": "FUJITSU_AC",
                    "Power": "On",
                    "Mode": "Fan",
                    "Temp": 10,
                }
            }
        )
        self.assertNotEqual(e.preset_mode, PRESET_FROST)

    async def test_partial_wire_fan_report_does_not_cancel_raw_frost(self):
        e = self.entity("fujitsu")
        receive = await self.receiver(e)
        await e.async_set_preset_mode(PRESET_FROST)
        await receive({"IRHVAC": {"Vendor": "FUJITSU_AC", "Mode": "Fan"}})
        self.assertEqual(e.preset_mode, PRESET_FROST)
        self.assertEqual(e.hvac_mode, "heat")
        await e.async_set_preset_mode("none")
        self.assertEqual(self.payload()["Mode"], "heat")

    async def test_normal_auto_fan_is_restored_after_frost(self):
        e = self.entity()
        await e.async_set_fan_mode("auto")
        await e.async_set_preset_mode(PRESET_FROST)
        await e.async_set_preset_mode("none")
        self.assertEqual(e.fan_mode, "auto")

    async def test_foreign_raw_protocol_cannot_activate_frost(self):
        e = self.entity("fujitsu")
        receive = await self.receiver(e)
        await receive(
            {
                "IrReceived": {
                    "Protocol": "GREE",
                    "Data": "0x1463001010FE0930410B001806122034",
                }
            }
        )
        self.assertNotEqual(e.preset_mode, PRESET_FROST)

    async def test_explicit_power_off_wins_over_a_raw_frost_match(self):
        e = self.entity("fujitsu")
        receive = await self.receiver(e)
        await receive(
            {
                "IrReceived": {
                    "Protocol": "FUJITSU_AC",
                    "Data": "0x1463001010FE0930410B001806122034",
                    "IRHVAC": {
                        "Vendor": "FUJITSU_AC",
                        "Power": "Off",
                        "Mode": "Fan",
                        "Temp": 10,
                    },
                }
            }
        )
        self.assertEqual(e.hvac_mode, "off")
        self.assertNotEqual(e.preset_mode, PRESET_FROST)

    async def test_store_only_raw_frost_does_not_transmit(self):
        e = self.entity("fujitsu")
        e._presets.activate()
        e._state_mode = "StoreOnly"
        await e.send_ir()
        self.publish.assert_not_awaited()
        self.assertEqual(e._state_mode, "SendStore")


class ConfigurationTests(unittest.TestCase):
    def test_raw_command_requires_topic_and_payload(self):
        for missing in ("topic", "payload"):
            config = deepcopy(CONFIGS["fujitsu"]["frost_protection"])
            config.pop(missing)
            with self.assertRaises(vol.Invalid):
                FROST_SCHEMA(config)

    def test_raw_frame_length_must_match_bits(self):
        config = deepcopy(CONFIGS["fujitsu"]["frost_protection"])
        config["payload"]["Bits"] = 64
        with self.assertRaises(vol.Invalid):
            FROST_SCHEMA(config)

    def test_temperature_configuration_must_be_finite(self):
        for field in ("temperature", "comfort_temperature"):
            for value in (float("nan"), float("inf"), True, None):
                with self.subTest(field=field, value=value):
                    config = deepcopy(CONFIGS["gree"]["frost_protection"])
                    config[field] = value
                    with self.assertRaises(vol.Invalid):
                        FROST_SCHEMA(config)

    def test_raw_options_cannot_be_attached_to_econo_command(self):
        config = deepcopy(CONFIGS["gree"]["frost_protection"])
        config["topic"] = "cmnd/gree_ac/irsend"
        with self.assertRaises(vol.Invalid):
            FROST_SCHEMA(config)

    def test_fahrenheit_frost_temperature_is_allowed(self):
        config = deepcopy(CONFIGS["gree"]["frost_protection"])
        config.update(temperature=46, comfort_temperature=68)
        self.assertEqual(FROST_SCHEMA(config)["temperature"], 46)


if __name__ == "__main__":
    unittest.main()
