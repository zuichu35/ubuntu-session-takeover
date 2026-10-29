#!/usr/bin/env python3
"""A persistent Wayland application used only inside the isolated test desktop."""
import gi
import json
import os
from pathlib import Path

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib

state_path = Path(os.environ['TAKEOVER_TEST_APP_STATE'])
app = Gtk.Application(application_id='local.takeover.TestApp')
clicks = 0


def activate(app):
    window = Gtk.ApplicationWindow(application=app, title='Persistent session test')
    window.set_default_size(800, 500)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
    box.set_margin_top(60)
    box.set_margin_start(80)
    box.set_margin_end(80)
    box.append(Gtk.Label(label='SAME SESSION — PipeWire + libei'))
    entry = Gtk.Entry(placeholder_text='Input must survive viewer reconnection')
    box.append(entry)
    button = Gtk.Button(label='Mouse input test')
    box.append(button)
    if os.environ.get('TAKEOVER_TEST_POINTER_DEBUG'):
        motion = Gtk.EventControllerMotion()
        motion.connect('motion', lambda _, x, y: print('pointer', x, y, flush=True))
        window.add_controller(motion)
        gesture = Gtk.GestureClick()
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect('pressed', lambda _, n, x, y: print('press', n, x, y, flush=True))
        gesture.connect('released', lambda _, n, x, y: print('release', n, x, y, flush=True))
        window.add_controller(gesture)

    def save(*args):
        state_path.write_text(json.dumps({'pid': os.getpid(), 'text': entry.get_text(), 'clicks': clicks}))

    def clicked(*args):
        global clicks
        clicks += 1
        save()

    entry.connect('changed', save)
    button.connect('clicked', clicked)
    window.set_child(box)
    if os.environ.get('TAKEOVER_TEST_VIEWPORT_CONTROL'):
        control = Path(os.environ['TAKEOVER_TEST_VIEWPORT_CONTROL'])
        overlay = Gtk.Overlay()
        window.set_child(None)
        overlay.set_child(box)
        probe = Gtk.Button(label='Viewport edge test')
        probe.set_halign(Gtk.Align.END)
        probe.set_valign(Gtk.Align.END)
        probe.set_margin_end(20)
        probe.set_margin_bottom(20)
        probe.set_size_request(240, 80)
        probe.set_visible(False)
        overlay.add_overlay(probe)
        window.set_child(overlay)

        def probe_clicked(*args):
            probe.set_label('Edge click received ' + str(clicks))
            if clicks % 2:
                probe.add_css_class('suggested-action')
            else:
                probe.remove_css_class('suggested-action')
            clicked()
        probe.connect('clicked', probe_clicked)

        last_command = None
        def check_control():
            nonlocal last_command
            command = control.read_text() if control.exists() else None
            if command is not None and command != last_command:
                last_command = command
                if command == 'windowed':
                    window.unfullscreen()
                else:
                    window.fullscreen()
                    probe.set_visible(True)
            return True
        GLib.timeout_add(100, check_control)
    css = Gtk.CssProvider()
    css.load_from_string('window {background: #164e63; color: white;} entry {min-height: 50px;} button {min-height: 50px;}')
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    window.present()
    entry.grab_focus()
    save()


app.connect('activate', activate)
app.run([])
