
import logging, asyncio, os

from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import (
    UnitOfTemperature,
    ATTR_UNIT_OF_MEASUREMENT,
)

from homeassistant.helpers.event import async_track_state_change
from homeassistant.exceptions import PlatformNotReady
from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity, ClimateEntityDescription
from homeassistant.util.unit_conversion import TemperatureConverter
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from homeassistant.components.climate.const import (
    HVACMode,
    HVACAction,
    ClimateEntityFeature
)

from .const import DOMAIN
from .nilan_cts600 import CTS600, NilanCTS600ProtocolError, findUSB, nilanString

if os.uname()[1] == 'x390':
    # development mockup device
    from .nilan_cts600 import CTS600Mockup as CTS600

_LOGGER = logging.getLogger(__name__)

DATA_KEY = "climate." + DOMAIN

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required("port"): vol.Coerce(str),
        vol.Optional("name", default="CTS600"): cv.string,
        vol.Optional("retries", default=2): vol.Coerce(int),
        vol.Optional("sensor_T15"): cv.entity_id,
    }
)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """ foo """
    _LOGGER.debug ("setup_entry: %s", entry.data)
    await async_setup_platform (hass, entry.data, async_add_entities)
    
async def async_setup_platform(
        hass: HomeAssistant,
        config: ConfigType,
        async_add_entities: AddEntitiesCallback,
        discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the platform."""
    # from .nilan_cts600 import CTS600, NilanCTS600ProtocolError, findUSB
    if DATA_KEY not in hass.data:
        hass.data[DATA_KEY] = {}

    _LOGGER.debug ("setup_platform: %s // %s", config, discovery_info)
    port = config.get ('port')
    if port == 'auto':
        port =  findUSB ()
    retries = int(config.get ('retries', 2))
    if not port:
        raise PlatformNotReady
    try:
        cts600 = CTS600 (port=port, logger=_LOGGER.debug)
        cts600.connect ()
    except Exception as e:
        _LOGGER.error ("Device connect failed for %s: %s", port, e)
        raise PlatformNotReady

    device = HaCTS600 (hass, cts600, config.get('name'),
                       retries=retries,
                       sensor_entity_id=config.get ('sensor_T15'),
                       )
    try:
        await device.initialize ()
    except Exception as e:
        _LOGGER.error ("Device init failed for %s: %s", port, e)
        raise PlatformNotReady

    hass.data[DATA_KEY][port] = device
    async_add_entities([device], update_before_add=True)

class HaCTS600 (ClimateEntity):
    """
    The main function of this class is to provide an async interface
    to the non-async code in nilan_cts600.py, so as to properly
    integrate with the HA eventloop.

    """
    _mode_map = {
        # Map CTS600 display text to HVACMode.
        'HEAT': HVACMode.HEAT,
        'COOL': HVACMode.COOL,
        'AUTO': HVACMode.AUTO,
        'OFF': HVACMode.OFF,
    }
    _mode_imap = {v:k for k,v in _mode_map.items()}
    _action_map = {
        # Map CTS600 display text to HVACAction.
        'HEATING': HVACAction.HEATING,
        'COOLING': HVACAction.COOLING,
        'OFF': HVACAction.OFF,
    }
    def __init__ (self, hass, cts600, name, retries=1, sensor_entity_id=None):
        if not hass:
            raise Exception ("No HASS object!")
        self.hass = hass
        self._name = name
        self._attr_unique_id = f"serial-{cts600.port}"
        
        self.cts600 = cts600
        self.retries = retries
        self._lock = asyncio.Lock()

        self._state = None
        self._last_on_operation = None
        self._fan_mode = None
        self._air_condition_model = None
        self._t15_fallback = None

        self.entity_description = ClimateEntityDescription(
            key='nilan_cts600',
            icon='mdi:hvac'
        )
        
        if sensor_entity_id:
            sensor_state = hass.states.get(sensor_entity_id)
            if sensor_state:
                self.hass.loop.create_task (self._update_T15_state (sensor_entity_id, None, sensor_state))
            async_track_state_change(hass, sensor_entity_id, self._update_T15_state)
        else:
            self._t15_fallback = 21

    async def _update_T15_state (self, entity_id, old_state, new_state):
        """ Update thermostat with latest (room) temperature from sensor."""
        if new_state.state is None or new_state.state in ["unknown", "unavailable"]:
            return
        if not self.hass:
            return

        sensor_unit = new_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UnitOfTemperature.CELSIUS
        value = TemperatureConverter.convert(
            float(new_state.state), sensor_unit, UnitOfTemperature.CELSIUS
        )
        await self.setT15 (value)

    @property
    def name (self):
        """Return the name of the climate device."""
        return self._name

    @property
    def min_temp(self):
        return 5

    @property
    def max_temp(self):
        return 30

    @property
    def target_temperature_step(self):
        """Return the target temperature step."""
        return 1

    @property
    def should_poll(self):
        """Return the polling state."""
        return True

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def supported_features(self):
        """Return the set of supported features."""
        return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE
    
    @property
    def hvac_modes(self):
        """Return the list of available hvac modes."""
        return [HVACMode.HEAT, HVACMode.COOL, HVACMode.AUTO, HVACMode.OFF]

    @property
    def hvac_mode(self):
        """Return hvac mode ie. heat, cool, fan only."""
        cts600mode = self.cts600.data.get ('mode')
        mode = self._mode_map.get(cts600mode, None) if cts600mode else None
        # _LOGGER.debug ('hvac mode %s -> %s', cts600mode, mode)
        return mode

    @property
    def hvac_action(self):
        """Return hvac action ie. heat, cool, off."""
        led = self.cts600.led()
        if led == 'on':
            cts600action = self.cts600.data.get ('status')
            action = self._action_map.get(cts600action, None) if cts600action else None
            _LOGGER.debug ('hvac action %s -> %s', cts600action, action)
            return action
        elif led == 'off':
            return HVACAction.IDLE
        else:
            return None

    @property
    def fan_modes (self):
        """Return the list of available fan modes."""
        return ['1', '2', '3', '4']

    @property
    def fan_mode (self):
        """Return the current fan speed."""
        flow = self.cts600.data.get ('flow', None)
        return str (flow) if flow else None
    
    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self.cts600.data.get('thermostat', None)

    @property
    def current_temperature (self):
        """Return the current temperature."""
        return self.cts600.getT15 ()

    async def async_set_temperature (self, temperature=None, **kwargs):
        """Set target temperature."""
        _LOGGER.debug ('set fan_temperature %s', temperature)
        await self.setThermostat (int(temperature))
    
    async def async_set_fan_mode (self, fan_mode):
        """Set the fan mode."""
        _LOGGER.debug ('set fan_mode %s', fan_mode)
        await self.setFlow (int(fan_mode))

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target hvac mode."""
        _LOGGER.debug ('set hvac_mode %s', hvac_mode)
        display = await self.resetMenu()
        current_mode = display.split()[0]
        if self._mode_map[current_mode] == hvac_mode:
            return
        elif hvac_mode == HVACMode.OFF:
            await self.key_off()
        else:
            if current_mode == 'OFF':
                await self.key_on()
            await self.setMode (self._mode_imap[hvac_mode])
        
    async def _call (self, method, *args):
        """Make a synchronous call to CTS600 by creating a job and
        then await that job. Use self._lock to serialize access to the
        underlying API. Also implement self.retries."""
        async with self._lock:
            for attempt in range(1, self.retries+1):
                _LOGGER.debug ("Call try %d: %s %s", attempt, method.__func__.__name__, args)
                try:
                    result = await self.hass.async_add_executor_job (method, *args)
                    break
                except (TimeoutError, NilanCTS600ProtocolError) as e:
                    _LOGGER.debug ("Exception %s: %s %s", e.__class__.__name__, method.__func__.__name__, args)
                    if not attempt<self.retries:
                        raise e
            _LOGGER.debug ("Call result: %s %s => %s", method.__func__.__name__, args, result)
            return result

    async def initialize (self):
        await self._call (self.cts600.initialize)
        await self._call (self.cts600.setLanguage, "ENGLISH")
        slaveID = self.cts600.slaveID()
        product = nilanString(slaveID['product'])
        self._attr_device_info = DeviceInfo(
            identifiers={
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.unique_id)
            },
            manufacturer="Nilan",
            model=product,
            sw_version=f"sw={slaveID['softwareVersion']},protocol={slaveID['protocolVersion']}",
        )
        _LOGGER.debug ("SlaveID: %s", self.cts600.slaveID())

    def key (self, key=0):
        return self._call (self.cts600.key, key)

    def key_on (self):
        return self._call (self.cts600.key_on)

    def key_off (self):
        return self._call (self.cts600.key_off)
    
    def updateData (self):
        return self._call (self.cts600.updateData)

    def setT15 (self, celcius):
        return self._call (self.cts600.setT15, celcius)

    def setFlow (self, flow):
        return self._call (self.cts600.setFlow, flow)

    def setThermostat (self, celsius):
        return self._call (self.cts600.setThermostat, celsius)

    def resetMenu (self):
        return self._call (self.cts600.resetMenu)

    def setMode (self, mode):
        return self._call (self.cts600.setMode, mode)

    async def async_update (self):
        if self._t15_fallback:
            await self.setT15 (self._t15_fallback)
            self._t15_fallback = None
        state = await self.updateData ()
        return state

    
