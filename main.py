# -*- coding: utf-8 -*-
"""
Rupor Events — Kivy Android app
Мобильная оболочка для парсера rupor.events.
"""

import datetime as dt
import os
import threading
import traceback

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.properties import BooleanProperty, ListProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

import rupor_parser

try:
    from android.permissions import request_permissions, Permission
except Exception:  # запуск на ПК
    request_permissions = None
    Permission = None


class EventCard(BoxLayout):
    def __init__(self, event, **kwargs):
        super().__init__(orientation="vertical", spacing=dp(6), padding=dp(10), **kwargs)
        self.size_hint_y = None
        self.bind(minimum_height=self.setter("height"))

        title = event.get("title") or "Без названия"
        self.add_widget(Label(
            text=f"[b]{title}[/b]",
            markup=True,
            halign="left",
            valign="top",
            size_hint_y=None,
            text_size=(None, None),
        ))

        when = event.get("date", "")
        if event.get("start"):
            when = event["start"][:16].replace("T", " ")
            if event.get("end"):
                when += " – " + event["end"][:16].replace("T", " ")

        venue = event.get("venue") or ""
        if event.get("address"):
            venue += f" — {event.get('address')}" if venue else event.get("address")

        lines = []
        if when:
            lines.append(f"Когда: {when}")
        if venue:
            lines.append(f"Где: {venue}")
        if event.get("genres"):
            lines.append("Жанры: " + ", ".join(event["genres"]))
        if event.get("price") is not None:
            lines.append(f"Цена: от {event.get('price')} ₽")
        if event.get("url"):
            lines.append(event["url"])

        desc = event.get("description")
        if desc:
            desc = desc.strip()
            if len(desc) > 700:
                desc = desc[:700].rstrip() + "…"
            lines.append("\n" + desc)

        info = Label(
            text="\n".join(lines),
            markup=False,
            halign="left",
            valign="top",
            size_hint_y=None,
        )
        info.bind(width=lambda inst, val: setattr(inst, "text_size", (val, None)))
        info.bind(texture_size=lambda inst, val: setattr(inst, "height", val[1]))
        self.add_widget(info)


class RuporRoot(BoxLayout):
    status_text = StringProperty("Готово")
    loading = BooleanProperty(False)
    events = ListProperty([])

    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=dp(8), padding=dp(10), **kwargs)
        self._build_ui()

    def _build_ui(self):
        title = Label(
            text="[b]Rupor Events Parser[/b]",
            markup=True,
            font_size="22sp",
            size_hint_y=None,
            height=dp(36),
        )
        self.add_widget(title)

        form = GridLayout(cols=2, spacing=dp(8), size_hint_y=None)
        form.bind(minimum_height=form.setter("height"))

        form.add_widget(Label(text="Город", size_hint_y=None, height=dp(42)))
        self.city_spinner = Spinner(
            text="spb",
            values=("spb", "msk", "nsk", "ekb", "kzn"),
            size_hint_y=None,
            height=dp(42),
        )
        form.add_widget(self.city_spinner)

        form.add_widget(Label(text="Дата", size_hint_y=None, height=dp(42)))
        self.date_input = TextInput(
            text="сегодня",
            hint_text="сегодня / завтра / 2026-07-04 / 4 июля",
            multiline=False,
            size_hint_y=None,
            height=dp(42),
        )
        form.add_widget(self.date_input)

        form.add_widget(Label(text="Жанр", size_hint_y=None, height=dp(42)))
        self.genre_input = TextInput(
            text="",
            hint_text="например: techno",
            multiline=False,
            size_hint_y=None,
            height=dp(42),
        )
        form.add_widget(self.genre_input)

        form.add_widget(Label(text="Детали", size_hint_y=None, height=dp(42)))
        details_box = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(42))
        self.details_checkbox = CheckBox(active=True, size_hint_x=None, width=dp(48))
        details_box.add_widget(self.details_checkbox)
        details_box.add_widget(Label(text="время, адрес, описание"))
        form.add_widget(details_box)

        self.add_widget(form)

        buttons = BoxLayout(orientation="horizontal", spacing=dp(8), size_hint_y=None, height=dp(48))
        self.load_button = Button(text="Загрузить")
        self.load_button.bind(on_press=lambda *_: self.load_events())
        self.export_button = Button(text="Экспорт TXT")
        self.export_button.bind(on_press=lambda *_: self.export_txt())
        buttons.add_widget(self.load_button)
        buttons.add_widget(self.export_button)
        self.add_widget(buttons)

        self.status_label = Label(
            text=self.status_text,
            size_hint_y=None,
            height=dp(32),
            halign="left",
            valign="middle",
        )
        self.status_label.bind(width=lambda inst, val: setattr(inst, "text_size", (val, None)))
        self.bind(status_text=lambda _, val: setattr(self.status_label, "text", val))
        self.add_widget(self.status_label)

        self.scroll = ScrollView()
        self.list_box = BoxLayout(orientation="vertical", spacing=dp(8), size_hint_y=None)
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.scroll.add_widget(self.list_box)
        self.add_widget(self.scroll)

    def set_loading(self, value: bool, text: str | None = None):
        self.loading = value
        self.load_button.disabled = value
        self.export_button.disabled = value
        if text is not None:
            self.status_text = text

    def load_events(self):
        if self.loading:
            return
        city = self.city_spinner.text.strip() or "spb"
        date_text = self.date_input.text.strip()
        genre = self.genre_input.text.strip()
        details = self.details_checkbox.active or bool(date_text)
        self.set_loading(True, "Загрузка афиши…")
        self.list_box.clear_widgets()

        def worker():
            try:
                html = rupor_parser.fetch_html(city)
                events = rupor_parser.parse_events(html)

                if genre:
                    needle = genre.lower()
                    events = [e for e in events if any(needle in g.lower() for g in e.get("genres", []))]

                if date_text:
                    target = rupor_parser.parse_date_arg(date_text, dt.date.today()).isoformat()
                    events = [e for e in events if e.get("date") == target]

                if details and events:
                    rupor_parser.enrich_with_details(events, delay=0.25, cache_dir=self.cache_dir())

                Clock.schedule_once(lambda *_: self.show_events(events))
            except Exception as exc:
                msg = f"Ошибка: {exc}\n\n{traceback.format_exc(limit=2)}"
                Clock.schedule_once(lambda *_: self.show_error(msg))

        threading.Thread(target=worker, daemon=True).start()

    def show_events(self, events):
        self.events = events
        self.list_box.clear_widgets()
        for event in events:
            self.list_box.add_widget(EventCard(event))
        self.set_loading(False, f"Событий: {len(events)}")

    def show_error(self, message):
        self.set_loading(False, "Ошибка загрузки")
        Popup(title="Ошибка", content=Label(text=message), size_hint=(0.92, 0.65)).open()

    def app_dir(self):
        app = App.get_running_app()
        return app.user_data_dir if app else os.getcwd()

    def cache_dir(self):
        path = os.path.join(self.app_dir(), ".rupor_cache")
        os.makedirs(path, exist_ok=True)
        return path

    def export_dir(self):
        candidates = [
            "/storage/emulated/0/Download",
            os.path.join(os.path.expanduser("~"), "Downloads"),
            self.app_dir(),
        ]
        for path in candidates:
            try:
                if os.path.isdir(path) and os.access(path, os.W_OK):
                    return path
            except Exception:
                pass
        return self.app_dir()

    def export_txt(self):
        if not self.events:
            Popup(title="Нет данных", content=Label(text="Сначала загрузи события."), size_hint=(0.8, 0.35)).open()
            return
        try:
            city = self.city_spinner.text.strip() or "spb"
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
            path = os.path.join(self.export_dir(), f"rupor_{city}_{stamp}.txt")
            rupor_parser.write_txt(list(self.events), path)
            Popup(title="Экспорт готов", content=Label(text=f"Файл сохранён:\n{path}"), size_hint=(0.9, 0.4)).open()
        except Exception as exc:
            self.show_error(str(exc))


class RuporEventsApp(App):
    def build(self):
        if request_permissions and Permission:
            request_permissions([
                Permission.INTERNET,
                Permission.WRITE_EXTERNAL_STORAGE,
                Permission.READ_EXTERNAL_STORAGE,
            ])
        return RuporRoot()


if __name__ == "__main__":
    RuporEventsApp().run()
