# Native frost protection presets

Some heat pumps have a native low-temperature heating function, often called
8°C heating, 10°C Heat, or frost protection. Sending an ordinary 8°C/10°C
setpoint does not necessarily select it: the protocol encoder may clamp normal
setpoints to a higher minimum.

The optional `frost_protection` configuration exposes a common thermostat UI
while using a device-specific command. Without this option, existing preset
behavior is unchanged.

| Preset/control | Behavior |
| --- | --- |
| Frost protection | Turn on native winter standby and show its fixed temperature |
| Comfort | Turn on normal heating at `comfort_temperature` |
| None | Exit frost protection and restore the previous normal target/fan mode |
| Temperature adjustment | Exit frost protection and use the selected normal target |
| Off or another HVAC mode | Exit the preset and perform the requested action |

When enabled, these presets replace the ordinary Away temperature-setback preset.
Comfort stays active until changed; it does not start a timer. A restart restores
the saved preset and a numeric target without transmitting IR or turning on an
appliance. A normal temperature change clears the preset. The fixed frost target
can be below `min_temp`: that setting still describes the normal adjustment range.

Temperatures use the native units selected by `celsius_mode`. Set Fahrenheit
values explicitly when `celsius_mode` is Off.

## Configuration

Add the block below to an otherwise working `tasmota_irhvac` climate definition.
Use the normal temperature limits supported by the heat pump, for example
`min_temp: 16`, `max_temp: 30`, and `target_temp: 20`.

### Opt-in GREE Econo mapping

```yaml
frost_protection:
  command: econo
  temperature: 8
  comfort_temperature: 20
```

Only use this mapping after verifying that `Econo: On` **in Heat mode** activates
frost protection on the specific GREE model/firmware. Econo normally describes
economy operation and is not a universal frost-protection flag. This mapping
comes from a local GREE/YAW1F customization; physical confirmation is still
required before treating that model as supported generally.

The mapping sends an IRHVAC heat command with Econo On and a valid normal encoded
setpoint. HA displays the configured frost temperature. Econo remains persistent
while the preset is selected, even if `toggle_list` originally included Econo.
Selecting Comfort, None, another mode, or a normal temperature clears the flag.
The existing `set_econo` action also selects or cancels this opt-in preset.

### A captured IRsend frame

```yaml
frost_protection:
  command: irsend
  temperature: 10
  comfort_temperature: 20
  topic: cmnd/fujitsu_ac/irsend
  payload:
    Protocol: FUJITSU_AC
    Bits: 128
    Data: "0x1463001010FE0930410B001806122034"
  match_state:
    Power: "On"
    Mode: Fan
    Temp: 10
```

The example frame was previously used successfully for 10°C frost protection on
one Fujitsu/ARRAH2E installation. Capture and verify the command for your own AC;
do not assume every model accepts the same frame. The topic must belong to that
AC's IR transmitter. Quote Data and include `0x`; its hex length must match Bits.

The captured frame is sent through IRsend. Comfort and normal adjustments use
the existing IRHVAC transport. No global script or dashboard-specific action is
required. `StoreOnly` does not transmit the raw frame.

Received frames with the configured protocol and Data identify frost protection.
Optional `match_state` also recognizes decoded IRHVAC feedback when the raw frame
is absent or varies. Every configured field must be present and match; string
comparison ignores case. In this example, the wire protocol encodes 10°C Heat as
Fan mode, while HA presents the preset as heating standby. Explicit Power Off
takes precedence over a frost match. Ordinary remote commands can clear the
preset; partial telemetry does not clear it without evidence of a mode change.

IR state is based on commands and feedback reported by the transmitter. Confirm
the appliance's indication and operation during the initial physical trial.

## Thermostat card

The built-in preset dropdown discovers the new choices from the entity:

```yaml
type: thermostat
entity: climate.example_ac
features:
  - type: climate-preset-modes
    style: dropdown
  - type: climate-hvac-modes
```

## Tests

Run with Home Assistant installed, from the repository root:

```sh
python -m unittest discover -s tests -p test_hvac_presets.py -v
```

The fixtures are self-contained and all MQTT publish/subscribe calls are mocked.
They do not require a user's configuration, MQTT broker, Tasmota device, or AC.

Alternatively, use the official Home Assistant container:

```sh
docker run --rm --network none --entrypoint python \
  -v "$PWD:/work:ro" -w /work \
  ghcr.io/home-assistant/home-assistant:2026.10.1 \
  -m unittest discover -s tests -p test_hvac_presets.py -v
```
