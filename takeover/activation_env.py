"""Capture the display environment Mutter registered on its private D-Bus."""
import os
from gi.repository import Gio, GLib
from takeover.common import BUS, RUNTIME, private_json


class Environment(Gio.Application):
    def do_activate(self):
        if os.environ.get('DBUS_SESSION_BUS_ADDRESS', '').split(',', 1)[0] != BUS:
            self.quit()
            return
        private_json(RUNTIME / 'activation-env.json',
                     {k: os.environ[k] for k in ('DISPLAY', 'XAUTHORITY') if k in os.environ})
        self.quit()


app = Environment(application_id='local.takeover.Environment', flags=Gio.ApplicationFlags.IS_SERVICE)
app.run([])
