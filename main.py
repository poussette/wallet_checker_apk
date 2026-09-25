"""Wallet Checker - Android app (Kivy).

Native port of the wallet_checker CLI: same providers/pricing modules,
wrapped in a small touch UI instead of argparse. Because this runs as a
real compiled app (not a page inside a browser), its network calls behave
exactly like the original Python script's -- no browser CORS policy
applies, so Solana token/staking lookups that fail from a browser page
work fine here.
"""

from __future__ import annotations

import os
import json
import threading
import traceback

# Android has no reliable default CA bundle path for `requests`/urllib3 to
# find on its own; point it at certifi's bundled one explicitly, before any
# HTTPS call is made.
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except Exception:
    pass

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

import report

SETTINGS_FILENAME = "wallet_checker_settings.json"

DEFAULT_CONFIG = """# Colle ici ta liste d'adresses, au meme format que addresses.txt :
#   <adresse>                  -> chaine auto-detectee
#   <chaine>,<adresse>         -> chaine forcee (bitcoin/ethereum/solana/multiversx)
#   [Un Label]                 -> regroupe les adresses qui suivent sous ce nom
#
# Exemple :
# [Perso]
# bitcoin,bc1q...
# ethereum,0x...
"""


class WalletCheckerApp(App):
    title = "Wallet Checker"

    def build(self):
        self.running = False
        self.last_results = None
        self.settings_path = os.path.join(self.user_data_dir, SETTINGS_FILENAME)
        self.settings = self.load_settings()

        root = BoxLayout(orientation="vertical", padding=10, spacing=8)

        top_bar = BoxLayout(size_hint=(1, None), height=48, spacing=8)
        top_bar.add_widget(Label(text="Wallet Checker", bold=True, font_size=20))
        settings_btn = Button(text="Parametres", size_hint=(None, 1), width=140)
        settings_btn.bind(on_release=self.open_settings)
        top_bar.add_widget(settings_btn)
        root.add_widget(top_bar)

        self.run_btn = Button(text="Verifier les wallets", size_hint=(1, None), height=52)
        self.run_btn.bind(on_release=self.on_run)
        root.add_widget(self.run_btn)

        self.status_label = Label(
            text="Ouvre Parametres pour coller ta liste d'adresses, puis appuie sur Verifier.",
            size_hint=(1, None), height=40,
        )
        root.add_widget(self.status_label)

        self.result_view = TextInput(
            text="", readonly=True, font_size=13,
            background_color=(0.06, 0.06, 0.08, 1), foreground_color=(0.9, 0.9, 0.92, 1),
        )
        scroll = ScrollView()
        scroll.add_widget(self.result_view)
        root.add_widget(scroll)

        export_bar = BoxLayout(size_hint=(1, None), height=48, spacing=8)
        self.copy_json_btn = Button(text="Copier JSON", disabled=True)
        self.copy_json_btn.bind(on_release=self.copy_json)
        self.copy_csv_btn = Button(text="Copier CSV", disabled=True)
        self.copy_csv_btn.bind(on_release=self.copy_csv)
        export_bar.add_widget(self.copy_json_btn)
        export_bar.add_widget(self.copy_csv_btn)
        root.add_widget(export_bar)

        return root

    # ---------------------------------------------------------------- settings

    def load_settings(self) -> dict:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("config_text", DEFAULT_CONFIG)
                data.setdefault("etherscan_key", "")
                data.setdefault("beacon_key", "")
                data.setdefault("show_unpriced", False)
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {
                "config_text": DEFAULT_CONFIG,
                "etherscan_key": "",
                "beacon_key": "",
                "show_unpriced": False,
            }

    def save_settings(self, data: dict) -> None:
        self.settings = data
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def open_settings(self, _instance):
        content = BoxLayout(orientation="vertical", padding=10, spacing=8)

        content.add_widget(Label(
            text="Configuration des wallets (meme format que addresses.txt)",
            size_hint=(1, None), height=30,
        ))
        config_input = TextInput(text=self.settings.get("config_text", DEFAULT_CONFIG), font_size=13)
        content.add_widget(config_input)

        keys_row = BoxLayout(size_hint=(1, None), height=44, spacing=8)
        keys_row.add_widget(Label(text="Cle Etherscan (optionnelle)", size_hint=(0.5, 1)))
        etherscan_input = TextInput(
            text=self.settings.get("etherscan_key", ""), multiline=False, password=True, size_hint=(0.5, 1),
        )
        keys_row.add_widget(etherscan_input)
        content.add_widget(keys_row)

        beacon_row = BoxLayout(size_hint=(1, None), height=44, spacing=8)
        beacon_row.add_widget(Label(text="Cle beaconcha.in (optionnelle)", size_hint=(0.5, 1)))
        beacon_input = TextInput(
            text=self.settings.get("beacon_key", ""), multiline=False, password=True, size_hint=(0.5, 1),
        )
        beacon_row.add_widget(beacon_input)
        content.add_widget(beacon_row)

        unpriced_row = BoxLayout(size_hint=(1, None), height=44, spacing=8)
        unpriced_checkbox = CheckBox(active=self.settings.get("show_unpriced", False), size_hint=(None, 1), width=44)
        unpriced_row.add_widget(unpriced_checkbox)
        unpriced_row.add_widget(Label(text="Afficher aussi les positions sans valeur connue"))
        content.add_widget(unpriced_row)

        buttons_row = BoxLayout(size_hint=(1, None), height=48, spacing=8)
        save_btn = Button(text="Enregistrer")
        cancel_btn = Button(text="Annuler")
        buttons_row.add_widget(cancel_btn)
        buttons_row.add_widget(save_btn)
        content.add_widget(buttons_row)

        popup = Popup(title="Parametres", content=content, size_hint=(0.95, 0.95))

        def do_save(_btn):
            self.save_settings({
                "config_text": config_input.text,
                "etherscan_key": etherscan_input.text.strip(),
                "beacon_key": beacon_input.text.strip(),
                "show_unpriced": unpriced_checkbox.active,
            })
            popup.dismiss()
            self.status_label.text = "Configuration enregistree. Appuie sur Verifier."

        save_btn.bind(on_release=do_save)
        cancel_btn.bind(on_release=lambda _b: popup.dismiss())
        popup.open()

    # ------------------------------------------------------------------- run

    def on_run(self, _instance):
        if self.running:
            return
        entries = report.parse_input_text(self.settings.get("config_text", ""))
        if not entries:
            self.status_label.text = "Aucune adresse dans la configuration -- ouvre Parametres pour en coller."
            return

        self.running = True
        self.run_btn.disabled = True
        self.copy_json_btn.disabled = True
        self.copy_csv_btn.disabled = True
        self.result_view.text = ""
        self.status_label.text = f"0 / {len(entries)} wallet(s) traite(s)..."

        threading.Thread(target=self._run_worker, args=(entries,), daemon=True).start()

    def _run_worker(self, entries):
        try:
            os.environ["ETHERSCAN_API_KEY"] = self.settings.get("etherscan_key", "") or ""
            os.environ["BEACONCHAIN_API_KEY"] = self.settings.get("beacon_key", "") or ""

            def progress(done, total):
                Clock.schedule_once(
                    lambda dt: setattr(self.status_label, "text", f"{done} / {total} wallet(s) traite(s)...")
                )

            results = report.fetch_all(entries, workers=4, on_progress=progress)

            try:
                from pricing import apply_pricing
                apply_pricing(results)
            except Exception:
                pass  # keep raw balances even if pricing fails entirely (e.g. offline)

            if not self.settings.get("show_unpriced", False):
                report.filter_unpriced(results)

            text = report.format_table(results)
            Clock.schedule_once(lambda dt: self._finish(results, text, None))
        except Exception:
            err = traceback.format_exc()
            Clock.schedule_once(lambda dt: self._finish(None, None, err))

    def _finish(self, results, text, error):
        self.running = False
        self.run_btn.disabled = False
        if error:
            self.status_label.text = "Erreur inattendue (voir le detail ci-dessous)."
            self.result_view.text = error
            return
        self.last_results = results
        self.result_view.text = text
        self.status_label.text = "Termine."
        self.copy_json_btn.disabled = False
        self.copy_csv_btn.disabled = False

    # ---------------------------------------------------------------- export

    def copy_json(self, _instance):
        if not self.last_results:
            return
        Clipboard.copy(report.build_json(self.last_results))
        self.status_label.text = "JSON copie dans le presse-papier."

    def copy_csv(self, _instance):
        if not self.last_results:
            return
        Clipboard.copy(report.build_csv(self.last_results))
        self.status_label.text = "CSV copie dans le presse-papier."


if __name__ == "__main__":
    WalletCheckerApp().run()
