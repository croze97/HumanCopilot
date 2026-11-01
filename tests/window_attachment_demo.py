#!/usr/bin/env python3
"""Manual GNOME attachment fixture: python3 tests/window_attachment_demo.py.

Requires the desktop's existing PyGObject/GTK4. An agent opens this fixture,
then asks the operator to use its buttons while a request targets
@{Attachment test · full}. No mouse or keyboard input is synthesized.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk


def activate(app):
    window = Gtk.ApplicationWindow(application=app, title="Attachment test")
    window.set_default_size(600, 430)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                  margin_top=28, margin_bottom=28, margin_start=28, margin_end=28)
    label = Gtk.Label(label="Window attachment test", wrap=True)
    box.append(label)
    box.append(Gtk.Label(
        label="The coloured frame should stay around this large window.\n"
              "The popup will reuse the original title; it must not steal the frame.",
        wrap=True))

    def rename(_button):
        window.set_title("Renamed document")
        label.set_text("Title changed — same window")

    def popup(_button):
        dialog = Gtk.Window(application=app, title="Attachment test popup",
                            transient_for=window, modal=False)
        dialog.set_default_size(330, 160)
        close = Gtk.Button(label="Close popup")
        close.connect("clicked", lambda _button: dialog.close())
        dialog.set_child(close)
        dialog.present()

    for text, callback in [("Rename window", rename), ("Open popup", popup)]:
        button = Gtk.Button(label=text)
        button.connect("clicked", callback)
        box.append(button)
    window.set_child(box)
    window.present()


if __name__ == "__main__":
    app = Gtk.Application(application_id="org.croze.AttachmentDemo")
    app.connect("activate", activate)
    app.run([])
