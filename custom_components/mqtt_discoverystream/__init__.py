"""Publish simple item state changes via MQTT."""
import json
import logging

from homeassistant.components import mqtt
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    SUPPORT_BRIGHTNESS,
    SUPPORT_EFFECT,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_INCLUDE,
    MATCH_ALL,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity import get_supported_features
from homeassistant.helpers.entityfilter import convert_include_exclude_filter
from homeassistant.helpers.event import async_track_state_change
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_B,
    ATTR_COLOR,
    ATTR_G,
    ATTR_H,
    ATTR_R,
    ATTR_S,
    ATTR_X,
    ATTR_Y,
    CONF_BASE_TOPIC,
    CONF_DISCOVERY_TOPIC,
    CONF_PUBLISH_ATTRIBUTES,
    CONF_PUBLISH_DISCOVERY,
    CONF_PUBLISH_TIMESTAMPS,
    DOMAIN,
)
from .schema import CONFIG_SCHEMA  # noqa: F401

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the MQTT state feed."""
    conf = config.get(DOMAIN)
    publish_filter = convert_include_exclude_filter(conf)
    has_includes = bool(conf.get(CONF_INCLUDE))
    base_topic = conf.get(CONF_BASE_TOPIC)
    discovery_topic = conf.get(CONF_DISCOVERY_TOPIC) or conf.get(CONF_BASE_TOPIC)
    publish_attributes = conf.get(CONF_PUBLISH_ATTRIBUTES)
    publish_timestamps = conf.get(CONF_PUBLISH_TIMESTAMPS)
    publish_discovery = conf.get(CONF_PUBLISH_DISCOVERY)
    if not base_topic.endswith("/"):
        base_topic = f"{base_topic}/"
    if not discovery_topic.endswith("/"):
        discovery_topic = f"{discovery_topic}/"
    hass.data[DOMAIN] = {}
    hass.data[DOMAIN][discovery_topic] = {}
    hass.data[DOMAIN][discovery_topic]["conf_published"] = []
    dev_reg = device_registry.async_get(hass)
    ent_reg = entity_registry.async_get(hass)

    async def _message_received(msg):
        """Handle new messages on MQTT."""
        explode_topic = msg.topic.split("/")
        domain = explode_topic[1]
        entity = explode_topic[2]
        element = explode_topic[3]

        _LOGGER.debug(
            "Message received: topic %s; payload: %s", {msg.topic}, {msg.payload}
        )
        if element == "set":
            if msg.payload == STATE_ON:
                await hass.services.async_call(
                    domain, SERVICE_TURN_ON, {ATTR_ENTITY_ID: f"{domain}.{entity}"}
                )
            elif msg.payload == STATE_OFF:
                await hass.services.async_call(
                    domain, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: f"{domain}.{entity}"}
                )
            else:
                _LOGGER.error(
                    'Invalid service for "set" - payload: %s for %s',
                    {msg.payload},
                    {entity},
                )
        if element == "set_light":
            if domain != "light":
                _LOGGER.error(
                    'Invalid domain for "set_light" - payload: %s for %s',
                    {msg.payload},
                    {entity},
                )
            else:
                payload_json = json.loads(msg.payload)
                service_payload = {
                    ATTR_ENTITY_ID: f"{domain}.{entity}",
                }
                if ATTR_TRANSITION in payload_json:
                    service_payload[ATTR_TRANSITION] = payload_json[ATTR_TRANSITION]

                if payload_json["state"] == "ON":
                    if ATTR_BRIGHTNESS in payload_json:
                        service_payload[ATTR_BRIGHTNESS] = payload_json[ATTR_BRIGHTNESS]
                    if ATTR_COLOR_TEMP in payload_json:
                        service_payload[ATTR_COLOR_TEMP] = payload_json[ATTR_COLOR_TEMP]
                    if ATTR_COLOR in payload_json:
                        if ATTR_H in payload_json[ATTR_COLOR]:
                            service_payload[ATTR_HS_COLOR] = [
                                payload_json[ATTR_COLOR][ATTR_H],
                                payload_json[ATTR_COLOR][ATTR_S],
                            ]
                        if ATTR_X in payload_json[ATTR_COLOR]:
                            service_payload[ATTR_XY_COLOR] = [
                                payload_json[ATTR_COLOR][ATTR_X],
                                payload_json[ATTR_COLOR][ATTR_Y],
                            ]
                        if ATTR_R in payload_json[ATTR_COLOR]:
                            service_payload[ATTR_RGB_COLOR] = [
                                payload_json[ATTR_COLOR][ATTR_R],
                                payload_json[ATTR_COLOR][ATTR_G],
                                payload_json[ATTR_COLOR][ATTR_B],
                            ]
                    await hass.services.async_call(
                        domain, SERVICE_TURN_ON, service_payload
                    )
                elif payload_json["state"] == "OFF":
                    await hass.services.async_call(
                        domain, SERVICE_TURN_OFF, service_payload
                    )
                else:
                    _LOGGER.error(
                        'Invalid state for "set_light" - payload: %s for %s',
                        {msg.payload},
                        {entity},
                    )

    async def _state_publisher(
        entity_id, old_state, new_state
    ):  # pylint: disable=unused-argument
        if new_state is None:
            return

        if not publish_filter(entity_id):
            return

        mybase = f"{base_topic}{entity_id.replace('.', '/')}/"

        if publish_timestamps:
            if new_state.last_updated:
                await mqtt.async_publish(
                    f"{mybase}last_updated", new_state.last_updated.isoformat(), 1, True
                )
            if new_state.last_changed:
                await mqtt.async_publish(
                    f"{mybase}last_changed", new_state.last_changed.isoformat(), 1, True
                )

        if publish_attributes:
            for key, val in new_state.attributes.items():
                encoded_val = json.dumps(val, cls=JSONEncoder)
                await mqtt.async_publish(mybase + key, encoded_val, 1, True)

        ent_parts = entity_id.split(".")
        ent_domain = ent_parts[0]
        ent_id = ent_parts[1]

        if (
            publish_discovery
            and entity_id not in hass.data[DOMAIN][discovery_topic]["conf_published"]
        ):
            config = {
                "uniq_id": f"mqtt_{entity_id}",
                "name": ent_id.replace("_", " ").title(),
                "stat_t": f"{mybase}state",
                "json_attr_t": f"{mybase}attributes",
                "avty_t": f"{mybase}availability",
            }
            if "device_class" in new_state.attributes:
                config["dev_cla"] = new_state.attributes["device_class"]
            if "unit_of_measurement" in new_state.attributes:
                config["unit_of_meas"] = new_state.attributes["unit_of_measurement"]
            if "state_class" in new_state.attributes:
                config["stat_cla"] = new_state.attributes["state_class"]

            publish_config = False
            if ent_domain == "sensor" and (
                has_includes or "device_class" in new_state.attributes
            ):
                publish_config = True

            elif ent_domain == "binary_sensor" and (
                has_includes or "device_class" in new_state.attributes
            ):
                config["pl_off"] = STATE_OFF
                config["pl_on"] = STATE_ON
                publish_config = True

            elif ent_domain == "switch":
                config["pl_off"] = STATE_OFF
                config["pl_on"] = STATE_ON
                config["cmd_t"] = f"{mybase}set"
                publish_config = True

            elif ent_domain == "device_tracker":
                publish_config = True

            elif ent_domain == "climate":
                config["current_temperature_topic"] = f"{mybase}attributes"
                config[
                    "current_temperature_template"
                ] = "{{ value_json.current_temperature }}"
                if "icon" in new_state.attributes:
                    config["icon"] = new_state.attributes["icon"]
                config["max_temp"] = new_state.attributes["max_temp"]
                config["min_temp"] = new_state.attributes["min_temp"]
                config["modes"] = new_state.attributes["hvac_modes"]
                config["mode_state_topic"] = f"{mybase}state"
                preset_modes = new_state.attributes["preset_modes"]
                if "none" in preset_modes:
                    preset_modes.remove("none")
                config["preset_modes"] = preset_modes
                config["preset_mode_command_topic"] = f"{mybase}preset_command"
                config["preset_mode_state_topic"] = f"{mybase}attributes"
                config["preset_mode_value_template"] = "{{ value_json.preset_mode }}"
                config["temperature_state_topic"] = f"{mybase}attributes"
                config["temperature_state_template"] = "{{ value_json.temperature }}"
                publish_config = True

            elif ent_domain == "light":
                del config["json_attr_t"]
                config["cmd_t"] = f"{mybase}set_light"
                config["schema"] = "json"

                supported_features = get_supported_features(hass, entity_id)
                if supported_features & SUPPORT_BRIGHTNESS:
                    config["brightness"] = True
                if supported_features & SUPPORT_EFFECT:
                    config["effect"] = True
                if "supported_color_modes" in new_state.attributes:
                    config["color_mode"] = True
                    config["supported_color_modes"] = new_state.attributes[
                        "supported_color_modes"
                    ]

                publish_config = True

            if publish_config:
                for entry in ent_reg.entities.values():
                    if entry.entity_id != entity_id:
                        continue
                    for device in dev_reg.devices.values():
                        if device.id != entry.device_id:
                            continue
                        config["dev"] = {}
                        if device.manufacturer:
                            config["dev"]["mf"] = device.manufacturer
                        if device.model:
                            config["dev"]["mdl"] = device.model
                        if device.name:
                            config["dev"]["name"] = device.name
                        if device.sw_version:
                            config["dev"]["sw"] = device.sw_version
                        if device.identifiers:
                            config["dev"]["ids"] = [id[1] for id in device.identifiers]
                        if device.connections:
                            config["dev"]["cns"] = device.connections

                encoded = json.dumps(config, cls=JSONEncoder)
                entity_disc_topic = (
                    f"{discovery_topic}{entity_id.replace('.', '/')}/config"
                )
                await mqtt.async_publish(entity_disc_topic, encoded, 1, True)
                hass.data[DOMAIN][discovery_topic]["conf_published"].append(entity_id)

        if publish_discovery:
            if ent_domain == "light":
                payload = {
                    "state": "ON" if new_state.state == STATE_ON else "OFF",
                }
                if "brightness" in new_state.attributes:
                    payload["brightness"] = new_state.attributes["brightness"]
                if "color_mode" in new_state.attributes:
                    payload["color_mode"] = new_state.attributes["color_mode"]
                if "color_temp" in new_state.attributes:
                    payload["color_temp"] = new_state.attributes["color_temp"]
                if "effect" in new_state.attributes:
                    payload["effect"] = new_state.attributes["effect"]

                color = {}
                if "hs_color" in new_state.attributes:
                    color["h"] = new_state.attributes["hs_color"][0]
                    color["s"] = new_state.attributes["hs_color"][1]
                if "xy_color" in new_state.attributes:
                    color["x"] = new_state.attributes["xy_color"][0]
                    color["x"] = new_state.attributes["xy_color"][1]
                if "rgb_color" in new_state.attributes:
                    color["r"] = new_state.attributes["rgb_color"][0]
                    color["g"] = new_state.attributes["rgb_color"][1]
                    color["b"] = new_state.attributes["rgb_color"][2]
                if color:
                    payload["color"] = color

                await mqtt.async_publish(
                    f"{mybase}state", json.dumps(payload, cls=JSONEncoder), 1, True
                )

                payload = (
                    "offline"
                    if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None)
                    else "online"
                )
                await mqtt.async_publish(f"{mybase}availability", payload, 1, True)
            else:
                payload = new_state.state
                await mqtt.async_publish(f"{mybase}state", payload, 1, True)

                payload = (
                    "offline"
                    if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None)
                    else "online"
                )
                await mqtt.async_publish(f"{mybase}availability", payload, 1, True)

                attributes = {}
                for key, val in new_state.attributes.items():
                    attributes[key] = val
                encoded = json.dumps(attributes, cls=JSONEncoder)
                await mqtt.async_publish(f"{mybase}attributes", encoded, 1, True)
        else:
            payload = new_state.state
            await mqtt.async_publish(f"{mybase}state", payload, 1, True)

    if publish_discovery:
        try:
            await hass.components.mqtt.async_subscribe(
                f"{base_topic}switch/+/set", _message_received
            )
            await hass.components.mqtt.async_subscribe(
                f"{base_topic}light/+/set_light", _message_received
            )
        except HomeAssistantError:
            _LOGGER.warning("MQTT Not ready")

    async_track_state_change(hass, MATCH_ALL, _state_publisher)
    return True
