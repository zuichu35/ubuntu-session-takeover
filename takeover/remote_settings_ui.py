"""Dashboard for the native remote desktop: live state, scale, monitors."""
import argparse
import json
import os
from pathlib import Path
import socket

from takeover.remote_settings import (SCALES, load_multi_monitor, load_scale, save_multi_monitor, save_scale,
                                      validate_scale)


def gate_path():
    return Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'ubuntu-native-takeover/gate.sock'


def apply_scale(percent):
    percent = validate_scale(percent)
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(12)
            connection.connect(str(gate_path()))
            connection.sendall(f'SCALE {percent}\n'.encode())
            line = connection.makefile('rb').readline(4096)
        if line == b'DENIED!\n':
            save_scale(percent)
            return 'Saved. The running service is still the old version; log in to Ubuntu again to use it.'
        result = json.loads(line)
        if not result.get('ok'):
            raise RuntimeError(result.get('error', 'Scale setting did not take effect'))
        return (f'Set to {percent}%. Takes effect immediately during a remote session; local display settings '
                'stay independent. A scale announced by the RDP client takes priority when it connects.')
    except (FileNotFoundError, ConnectionRefusedError):
        save_scale(percent)
        return f'Saved {percent}%; used at the next remote takeover unless the RDP client announces its own scale.'


def query_status():
    """Live state from the running service, or None when it is not running."""
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3)
            connection.connect(str(gate_path()))
            connection.sendall(b'STATUS\n')
            status = json.loads(connection.makefile('rb').readline(4096))
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def saved_settings():
    """The stored fallbacks; a damaged file reads as the defaults' absence."""
    try:
        return load_scale(), load_multi_monitor()
    except ValueError:
        return None, None


def describe(status):
    """(headline, detail) of a STATUS reply for the dashboard."""
    if status is None:
        return 'Service not running', 'Changes are saved and used at the next remote takeover.'
    if not status.get('remote'):
        return 'No remote session', 'The desktop is used locally. Changes are saved for the next remote session.'
    size = status.get('viewport')
    parts = [f'{size[0]} × {size[1]}' if size else 'resolution of the local display']
    if status.get('scale_percent'):
        parts.append(f"{status['scale_percent']}%")
    monitors = 1 + int(status.get('remote_only_monitors') or 0)
    parts.append('1 monitor' if monitors == 1 else f'{monitors} monitors')
    client = status.get('client_scale')
    detail = (f'The client announced {client}%. A scale chosen here lasts until the client announces another one.'
              if client else 'The client announced no scale; the saved one is used when it connects.')
    return 'Remote session active: ' + ', '.join(parts), detail


def shortcut_files(root, home):
    """The UST launcher and the `ust` command, as a fresh setup writes them."""
    command = '/usr/bin/python3 ' + str(Path(root) / 'scripts/remote-settings.py')
    return {
        Path(home) / '.local/share/applications/ubuntu-native-takeover-settings.desktop':
            '[Desktop Entry]\nType=Application\nName=UST\nGenericName=Ubuntu Session Takeover\n'
            'Comment=Live state, scale and monitors of the remotely taken-over desktop\n'
            'Exec=' + command + '\n'
            'Icon=preferences-desktop-display\nTerminal=false\nCategories=Settings;DesktopSettings;\n'
            'Keywords=ust;takeover;remote;rdp;scale;monitor;\n',
        Path(home) / '.local/bin/ust': '#!/bin/sh\nexec ' + command + ' "$@"\n',
        # Unchanged in function; rewritten so that installations made by the
        # earlier Chinese-language setup show an English name.
        Path(home) / '.config/autostart/ubuntu-native-takeover.desktop':
            '[Desktop Entry]\nType=Application\nName=Native Desktop Remote Takeover\n'
            'Exec=/usr/bin/systemctl --user start ubuntu-native-takeover.service\n'
            'OnlyShowIn=GNOME;\nX-GNOME-Autostart-enabled=true\n',
    }


def install_shortcut(root=None, home=None):
    """Give an existing installation the UST launcher and the `ust` command.

    Only files this package installed, or new ones, are written; the install
    manifest is updated so that disabling the takeover still removes them.
    """
    import hashlib
    root = Path(root or Path(__file__).resolve().parents[1])
    home = Path(home or Path.home())
    manifest_path = home / '.config/ubuntu-native-takeover/install.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    known = (manifest or {}).get('installed_files', {})
    files = shortcut_files(root, home)
    autostart = home / '.config/autostart/ubuntu-native-takeover.desktop'
    if not autostart.exists():
        del files[autostart]  # Never enable the takeover where it is not set up.
    for path in files:
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != known.get(str(path)):
            raise RuntimeError(f'{path} exists and was not installed by this package; not overwritten')
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if text.startswith('#!'):
            path.chmod(0o755)
        if manifest is not None:
            manifest.setdefault('files', {}).setdefault(str(path), None)
            manifest.setdefault('installed_files', {})[str(path)] = hashlib.sha256(text.encode()).hexdigest()
    if manifest is not None:
        temporary = manifest_path.with_name('.install.json.new')
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        os.replace(temporary, manifest_path)
    return [str(path) for path in files]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scale', type=int, choices=SCALES)
    parser.add_argument('--multi-monitor', choices=('on', 'off'),
                        help='Extra client monitors as remote-only monitors; applies when the remote backend next starts')
    parser.add_argument('--status', action='store_true', help='Print the live state as JSON')
    parser.add_argument('--install-shortcut', action='store_true',
                        help='Add the UST launcher and the `ust` command to an existing installation and '
                             'refresh the names of its entries')
    args = parser.parse_args()
    if args.install_shortcut:
        print('Installed: ' + ', '.join(install_shortcut()))
        print('Open "UST" from the application menu, or run `ust` in a terminal.')
        return
    if args.multi_monitor is not None:
        save_multi_monitor(args.multi_monitor == 'on')
        print(f'Multiple monitors {args.multi_monitor}; takes effect when the remote backend next starts '
              '(systemctl --user restart ubuntu-native-takeover.service while idle, or log in again).')
    if args.scale is not None:
        print(apply_scale(args.scale))
    if args.status:
        scale, multi_monitor = saved_settings()
        print(json.dumps({'service': query_status(), 'saved_scale_percent': scale,
                          'saved_multi_monitor': multi_monitor}, indent=2))
    if args.scale is not None or args.multi_monitor is not None or args.status:
        return
    run_dashboard()


def run_dashboard():
    import threading
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, GLib

    app = Gtk.Application(application_id='local.takeover.RemoteSettings')

    def activate(app):
        window = Gtk.ApplicationWindow(application=app, title='UST – Ubuntu Session Takeover')
        window.set_default_size(520, 0)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(box, 'set_margin_' + side)(24)

        def label(text='', *classes):
            widget = Gtk.Label(label=text, wrap=True, xalign=0)
            for name in classes:
                widget.add_css_class(name)
            box.append(widget)
            return widget

        headline = label('', 'title-3')
        detail = label('', 'dim-label')
        box.append(Gtk.Separator())

        label('Scale', 'heading')
        row = Gtk.Box(spacing=0)
        row.add_css_class('linked')
        buttons = {}
        for percent in SCALES:
            button = Gtk.ToggleButton(label=f'{percent}%', hexpand=True)
            if buttons:
                button.set_group(next(iter(buttons.values())))
            buttons[percent] = button
            row.append(button)
        box.append(row)
        message = label('Applies immediately to a running remote session.', 'dim-label')
        box.append(Gtk.Separator())

        label('Monitors', 'heading')
        monitors = Gtk.Box(spacing=12)
        monitors.append(Gtk.Label(label='Use every monitor of the client', xalign=0, hexpand=True))
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        monitors.append(switch)
        box.append(monitors)
        monitors_note = label('Takes effect when the remote backend next starts: after the next login, or '
                              'after restarting ubuntu-native-takeover.service while nobody is connected.',
                              'dim-label')

        state = {'updating': False, 'busy': False}

        def show(status):
            """Reflect the live state without reporting it back as a change."""
            state['updating'] = True
            head, text = describe(status)
            headline.set_text(head)
            detail.set_text(text)
            saved_scale, saved_multi = saved_settings()
            current = (status or {}).get('scale_percent') if (status or {}).get('remote') else saved_scale
            if current in buttons and not state['busy']:
                buttons[current].set_active(True)
            if saved_multi is not None:
                switch.set_active(saved_multi)
            if status and status.get('multi_monitor_supported') is False:
                monitors_note.set_text('The running compositor shows one monitor only; log in to Ubuntu again '
                                       'after an update to use every monitor of the client.')
            state['updating'] = False
            return False

        def refresh():
            if not state['busy']:
                threading.Thread(target=lambda: GLib.idle_add(show, query_status()), daemon=True).start()
            return True

        def chosen(button, percent):
            if state['updating'] or not button.get_active():
                return
            state['busy'] = True
            message.set_text(f'Applying {percent}%…')

            def work():
                try:
                    text = apply_scale(percent)
                except Exception as error:
                    text = 'Could not apply: ' + str(error)

                def done():
                    state['busy'] = False
                    message.set_text(text)
                    refresh()
                    return False
                GLib.idle_add(done)
            threading.Thread(target=work, daemon=True).start()

        def switched(widget, _parameter):
            if state['updating']:
                return
            try:
                save_multi_monitor(widget.get_active())
            except Exception as error:
                monitors_note.set_text('Could not save: ' + str(error))

        for percent, button in buttons.items():
            button.connect('toggled', chosen, percent)
        switch.connect('notify::active', switched)
        show(query_status())
        GLib.timeout_add_seconds(2, refresh)
        window.set_child(box)
        window.present()

    app.connect('activate', activate)
    app.run([])


if __name__ == '__main__':
    main()
