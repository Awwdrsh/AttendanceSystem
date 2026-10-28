"""Fixed values of the devices slice, shared by services, serializers and forms."""
import re

ROLES = ('hr', 'admin')
# Punch sources that are not terminals: never listed on the Devices page and never disabled from it.
PSEUDO_VENDORS = ('mobile', 'kiosk', 'manual')
COMMANDS = ('CHECK', 'INFO', 'REBOOT', 'LOG', 'DATA QUERY USERINFO', 'CLEAR LOG')
# The command picker on the Devices page offers these (CLEAR LOG stays API-only, as in the TS page).
PAGE_COMMANDS = ('CHECK', 'INFO', 'LOG', 'REBOOT', 'DATA QUERY USERINFO')
VENDOR = {'zkteco': 'ZKTeco', 'hikvision': 'Hikvision', 'suprema': 'Suprema'}
PROTOCOL = {'adms': 'ADMS push', 'isapi': 'ISAPI'}

# Terminal PINs are plain digits/letters; anything else could smuggle extra fields or commands into the device
# protocol. \Z, not $: Python's $ also matches before a trailing newline.
PIN_RE = re.compile(r'^[A-Za-z0-9]{1,24}\Z')
PIN_MESSAGE = 'PIN must be 1 to 24 letters or digits'

# A terminal that has not been heard from for this long is offline.
OFFLINE_AFTER_SECONDS = 120
