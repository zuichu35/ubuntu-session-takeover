"""Small control panel for the native remote desktop's independent scale."""
import argparse
import json
import os
from pathlib import Path
import socket

from takeover.remote_settings import SCALES, load_scale, save_scale, validate_scale


def apply_scale(percent):
    percent = validate_scale(percent)
    gate = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'ubuntu-native-takeover/gate.sock'
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(12)
            connection.connect(str(gate))
            connection.sendall(f'SCALE {percent}\n'.encode())
            line = connection.makefile('rb').readline(4096)
        if line == b'DENIED!\n':
            save_scale(percent)
            return '已保存。当前服务仍是旧版，请重新登录 Ubuntu 后使用。'
        result = json.loads(line)
        if not result.get('ok'):
            raise RuntimeError(result.get('error', '缩放设置未生效'))
        return f'已设为 {percent}%。远程期间立即生效；本地显示设置保持独立。'
    except (FileNotFoundError, ConnectionRefusedError):
        save_scale(percent)
        return f'已保存 {percent}%，下次远程接管时生效。'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scale', type=int, choices=SCALES)
    args = parser.parse_args()
    if args.scale is not None:
        print(apply_scale(args.scale))
        return
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, GLib
    import threading

    app = Gtk.Application(application_id='local.takeover.RemoteSettings')

    def activate(app):
        window = Gtk.ApplicationWindow(application=app, title='远程桌面设置')
        window.set_default_size(460, 230)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(box, 'set_margin_' + side)(24)
        box.append(Gtk.Label(label='远程桌面缩放', xalign=0))
        selector = Gtk.DropDown.new_from_strings([f'{value}%' for value in SCALES])
        selector.set_selected(SCALES.index(load_scale()))
        box.append(selector)
        status = Gtk.Label(label='调整文字和界面大小；断开远程后恢复本地显示布局。', wrap=True, xalign=0)
        box.append(status)
        button = Gtk.Button(label='应用')
        button.add_css_class('suggested-action')
        box.append(button)

        def completed(message):
            status.set_text(message)
            button.set_sensitive(True)
            return False

        def clicked(*_):
            value = SCALES[selector.get_selected()]
            button.set_sensitive(False)
            def work():
                try:
                    message = apply_scale(value)
                except Exception as error:
                    message = '未能应用：' + str(error)
                GLib.idle_add(completed, message)
            threading.Thread(target=work, daemon=True).start()
        button.connect('clicked', clicked)
        window.set_child(box)
        window.present()

    app.connect('activate', activate)
    app.run([])


if __name__ == '__main__':
    main()
