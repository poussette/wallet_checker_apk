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
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.utils import escape_markup

import report

APP_VERSION = "0.2"
SETTINGS_FILENAME = "wallet_checker_settings.json"

# Monospace font shipped with Kivy (used for the config editor).
try:
    import kivy

    MONO_FONT = os.path.join(kivy.kivy_data_dir, "fonts", "RobotoMono-Regular.ttf")
    if not os.path.exists(MONO_FONT):
        MONO_FONT = "Roboto"
except Exception:
    MONO_FONT = "Roboto"

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

DEFAULT_SETTINGS = {
    "config_text": DEFAULT_CONFIG,
    "etherscan_key": "",
    "beacon_key": "",
    "show_unpriced": False,
    "show_dust": False,
}

# Colours (RGBA)
BG_GROUP = (0.17, 0.22, 0.34, 1)
BG_WALLET = (0.14, 0.15, 0.20, 1)
BG_BODY = (0.08, 0.09, 0.12, 1)
BG_SUMMARY = (0.10, 0.20, 0.16, 1)
GREY = "9aa0aa"
RED = "ff6b6b"
ORANGE = "ffb347"


# ------------------------------------------------------------------ formatting

def fmt_usd(v):
    return "-" if v is None else f"${v:,.2f}"


def fmt_eur(v):
    return "-" if v is None else f"€{v:,.2f}"


def fmt_amount(x):
    if x is None:
        return "-"
    s = f"{x:,.8f}" if abs(x) < 1 else f"{x:,.4f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def short_addr(a: str) -> str:
    return a if len(a) <= 16 else f"{a[:7]}...{a[-6:]}"


def esc(s) -> str:
    return escape_markup(str(s))


# --------------------------------------------------------------------- widgets

class WrapLabel(Label):
    """Label that wraps its text to its own width and grows in height."""

    def __init__(self, min_height=0, **kwargs):
        kwargs.setdefault("halign", "left")
        kwargs.setdefault("valign", "middle")
        kwargs.setdefault("font_size", sp(14))
        kwargs.setdefault("size_hint", (1, None))
        super().__init__(**kwargs)
        self._min_h = min_height
        self.bind(width=self._update, texture_size=self._update)

    def _update(self, *_):
        self.text_size = (self.width, None)
        self.height = max(self.texture_size[1] + dp(8), self._min_h)


class Panel(BoxLayout):
    """Vertical box that sizes itself to its content and paints a background."""

    def __init__(self, bg=BG_BODY, **kwargs):
        kwargs.setdefault("orientation", "vertical")
        kwargs.setdefault("size_hint", (1, None))
        super().__init__(**kwargs)
        with self.canvas.before:
            self._color = Color(*bg)
            self._rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._sync_rect, size=self._sync_rect)
        self.bind(minimum_height=self.setter("height"))

    def _sync_rect(self, *_):
        self._rect.pos = self.pos
        self._rect.size = self.size


class Row(BoxLayout):
    """One position: description on the left, USD/EUR values on the right."""

    def __init__(self, left_markup, right_markup, **kwargs):
        super().__init__(
            orientation="horizontal", size_hint=(1, None), spacing=dp(8),
            padding=(dp(12), dp(2), dp(12), dp(2)), **kwargs,
        )
        self.lbl_left = WrapLabel(text=left_markup, markup=True, size_hint=(0.58, None), font_size=sp(13))
        self.lbl_right = WrapLabel(
            text=right_markup, markup=True, size_hint=(0.42, None), halign="right", font_size=sp(13),
        )
        for w in (self.lbl_left, self.lbl_right):
            w.pos_hint = {"top": 1}
            w.bind(height=self._sync)
            self.add_widget(w)
        self._sync()

    def _sync(self, *_):
        self.height = max(self.lbl_left.height, self.lbl_right.height) + dp(4)


class Section(BoxLayout):
    """A tappable header that expands/collapses a body built on first open."""

    def __init__(self, title, subtitle, build_body, bg, indent=0, **kwargs):
        super().__init__(
            orientation="vertical", size_hint=(1, None), spacing=dp(2),
            padding=(dp(indent), 0, 0, 0), **kwargs,
        )
        self.bind(minimum_height=self.setter("height"))
        self._title = title
        self._subtitle = subtitle
        self._build_body = build_body
        self._body = None
        self._open = False
        self.header = Button(
            markup=True, size_hint=(1, None), height=dp(64),
            background_normal="", background_down="", background_color=bg,
            halign="left", valign="middle", padding=(dp(12), dp(6)), font_size=sp(14),
        )
        self.header.bind(size=lambda w, _s: setattr(w, "text_size", (w.width - dp(24), w.height)))
        self.header.bind(on_release=self.toggle)
        self.add_widget(self.header)
        self._refresh()

    def _refresh(self):
        mark = "-" if self._open else "+"
        self.header.text = f"{mark}  {self._title}\n[size={int(sp(12))}][color={GREY}]{self._subtitle}[/color][/size]"

    def toggle(self, *_):
        if self._open:
            self.remove_widget(self._body)
            self._open = False
        else:
            if self._body is None:
                self._body = self._build_body()
            self.add_widget(self._body)
            self._open = True
        self._refresh()


# ------------------------------------------------------------------------- app

class WalletCheckerApp(App):
    title = "Wallet Checker"

    def build(self):
        self.running = False
        self.last_results = None
        self.last_text = ""
        self.settings_path = os.path.join(self.user_data_dir, SETTINGS_FILENAME)
        self.settings = self.load_settings()

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        top_bar = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        top_bar.add_widget(Label(
            text=f"Wallet Checker  [size={int(sp(11))}][color={GREY}]v{APP_VERSION}[/color][/size]",
            markup=True, bold=True, font_size=sp(20), halign="left",
        ))
        settings_btn = Button(text="Parametres", size_hint=(None, 1), width=dp(130), font_size=sp(14))
        settings_btn.bind(on_release=self.open_settings)
        top_bar.add_widget(settings_btn)
        root.add_widget(top_bar)

        self.run_btn = Button(text="Verifier les wallets", size_hint=(1, None), height=dp(56), font_size=sp(16))
        self.run_btn.bind(on_release=self.on_run)
        root.add_widget(self.run_btn)

        self.status_label = WrapLabel(
            text="Ouvre Parametres pour coller ta liste d'adresses, puis appuie sur Verifier.",
            min_height=dp(40),
        )
        root.add_widget(self.status_label)

        # Scrollable results: summary card + collapsible label/wallet sections.
        self.results_box = BoxLayout(
            orientation="vertical", size_hint=(1, None), spacing=dp(6),
            padding=(0, 0, 0, dp(16)),
        )
        self.results_box.bind(minimum_height=self.results_box.setter("height"))
        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(4))
        self.scroll.add_widget(self.results_box)
        root.add_widget(self.scroll)

        export_bar = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        self.copy_text_btn = Button(text="Copier texte", disabled=True, font_size=sp(13))
        self.copy_text_btn.bind(on_release=self.copy_text)
        self.copy_json_btn = Button(text="Copier JSON", disabled=True, font_size=sp(13))
        self.copy_json_btn.bind(on_release=self.copy_json)
        self.copy_csv_btn = Button(text="Copier CSV", disabled=True, font_size=sp(13))
        self.copy_csv_btn.bind(on_release=self.copy_csv)
        for b in (self.copy_text_btn, self.copy_json_btn, self.copy_csv_btn):
            export_bar.add_widget(b)
        root.add_widget(export_bar)

        return root

    # ---------------------------------------------------------------- settings

    def load_settings(self) -> dict:
        data = dict(DEFAULT_SETTINGS)
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return data

    def save_settings(self, data: dict) -> None:
        self.settings = data
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def open_settings(self, _instance):
        content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        content.add_widget(WrapLabel(
            text="Configuration des wallets (meme format que addresses.txt)",
            min_height=dp(30),
        ))
        config_input = TextInput(
            text=self.settings.get("config_text", DEFAULT_CONFIG),
            font_size=sp(12), font_name=MONO_FONT,
        )
        content.add_widget(config_input)

        keys_row = BoxLayout(size_hint=(1, None), height=dp(48), spacing=dp(8))
        keys_row.add_widget(WrapLabel(text="Cle Etherscan (optionnelle)", size_hint=(0.5, 1), font_size=sp(12)))
        etherscan_input = TextInput(
            text=self.settings.get("etherscan_key", ""), multiline=False, password=True, size_hint=(0.5, 1),
        )
        keys_row.add_widget(etherscan_input)
        content.add_widget(keys_row)

        beacon_row = BoxLayout(size_hint=(1, None), height=dp(48), spacing=dp(8))
        beacon_row.add_widget(WrapLabel(text="Cle beaconcha.in (optionnelle)", size_hint=(0.5, 1), font_size=sp(12)))
        beacon_input = TextInput(
            text=self.settings.get("beacon_key", ""), multiline=False, password=True, size_hint=(0.5, 1),
        )
        beacon_row.add_widget(beacon_input)
        content.add_widget(beacon_row)

        unpriced_row = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        unpriced_checkbox = CheckBox(active=self.settings.get("show_unpriced", False), size_hint=(None, 1), width=dp(44))
        unpriced_row.add_widget(unpriced_checkbox)
        unpriced_row.add_widget(WrapLabel(
            text="Afficher aussi les positions sans valeur connue", size_hint=(1, 1), font_size=sp(12),
        ))
        content.add_widget(unpriced_row)

        dust_row = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        dust_checkbox = CheckBox(active=self.settings.get("show_dust", False), size_hint=(None, 1), width=dp(44))
        dust_row.add_widget(dust_checkbox)
        dust_row.add_widget(WrapLabel(
            text="Afficher aussi les positions de moins de 1 centime", size_hint=(1, 1), font_size=sp(12),
        ))
        content.add_widget(dust_row)

        buttons_row = BoxLayout(size_hint=(1, None), height=dp(52), spacing=dp(8))
        save_btn = Button(text="Enregistrer", font_size=sp(15))
        cancel_btn = Button(text="Annuler", font_size=sp(15))
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
                "show_dust": dust_checkbox.active,
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
        for b in (self.copy_text_btn, self.copy_json_btn, self.copy_csv_btn):
            b.disabled = True
        self.results_box.clear_widgets()
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

            priced_ok = True
            try:
                from pricing import apply_pricing
                apply_pricing(results)
            except Exception:
                priced_ok = False  # offline / pricing down: keep raw balances

            if priced_ok and not self.settings.get("show_unpriced", False):
                report.filter_unpriced(results)
                if not self.settings.get("show_dust", False):
                    report.filter_dust(results)

            text = report.format_table(results)
            Clock.schedule_once(lambda dt: self._finish(results, text, None, priced_ok))
        except Exception:
            err = traceback.format_exc()
            Clock.schedule_once(lambda dt: self._finish(None, None, err, True))

    def _finish(self, results, text, error, priced_ok):
        self.running = False
        self.run_btn.disabled = False
        self.results_box.clear_widgets()
        if error:
            self.status_label.text = "Erreur inattendue (detail ci-dessous)."
            self.results_box.add_widget(WrapLabel(
                text=f"[color={RED}]{esc(error)}[/color]", markup=True, font_size=sp(11),
            ))
            return
        self.last_results = results
        self.last_text = text
        try:
            self.render_results(results)
        except Exception:
            self.results_box.add_widget(WrapLabel(
                text=f"[color={RED}]{esc(traceback.format_exc())}[/color]", markup=True, font_size=sp(11),
            ))
        msg = "Termine."
        if not priced_ok:
            msg += " (prix indisponibles : soldes bruts affiches)"
        self.status_label.text = msg
        for b in (self.copy_text_btn, self.copy_json_btn, self.copy_csv_btn):
            b.disabled = False

    # ---------------------------------------------------------------- results

    def render_results(self, results):
        box = self.results_box
        priced = [w for w in results if not w.error and w.total_usd is not None]
        errors = [w for w in results if w.error]

        # Summary card
        summary = Panel(bg=BG_SUMMARY, padding=dp(12), spacing=dp(2))
        grand_usd = sum(w.total_usd for w in priced)
        grand_eur = sum((w.total_eur or 0.0) for w in priced)
        summary.add_widget(WrapLabel(
            text=f"[color={GREY}]TOTAL GENERAL[/color]", markup=True, font_size=sp(12),
        ))
        summary.add_widget(WrapLabel(
            text=f"[b]{fmt_usd(grand_usd)}[/b]", markup=True, font_size=sp(28),
        ))
        summary.add_widget(WrapLabel(
            text=f"[b]{fmt_eur(grand_eur)}[/b]", markup=True, font_size=sp(20),
        ))
        line = f"{len(priced)} wallet(s) valorise(s) sur {len(results)}"
        if errors:
            line += f"  [color={RED}]{len(errors)} en erreur[/color]"
        summary.add_widget(WrapLabel(
            text=f"[color={GREY}]{line}[/color]", markup=True, font_size=sp(12),
        ))
        box.add_widget(summary)

        # Group wallets by label, keeping the order of the config.
        groups: dict = {}
        for w in results:
            groups.setdefault(w.label, []).append(w)
        label_totals = report.compute_label_totals(results)

        for label, wallets in groups.items():
            if label and label in label_totals:
                t = label_totals[label]
                usd, eur = (t["usd"], t["eur"]) if t["priced"] else (None, None)
            else:
                ok = [w for w in wallets if not w.error and w.total_usd is not None]
                usd = sum(w.total_usd for w in ok) if ok else None
                eur = sum((w.total_eur or 0.0) for w in ok) if ok else None
            name = esc(label) if label else "Sans label"
            sub = f"{fmt_usd(usd)} / {fmt_eur(eur)}   ({len(wallets)} wallet(s))"
            box.add_widget(Section(
                f"[b]{name}[/b]", sub,
                build_body=lambda ws=wallets: self._group_body(ws),
                bg=BG_GROUP,
            ))

    def _group_body(self, wallets):
        body = Panel(bg=(0, 0, 0, 0), spacing=dp(4))
        for w in wallets:
            if w.error:
                sub = f"[color={RED}]erreur[/color]"
            else:
                sub = f"{fmt_usd(w.total_usd)} / {fmt_eur(w.total_eur)}"
            body.add_widget(Section(
                f"[b]{esc(w.chain)}[/b]  {esc(short_addr(w.address))}", sub,
                build_body=lambda w=w: self._wallet_body(w),
                bg=BG_WALLET, indent=10,
            ))
        return body

    def _wallet_body(self, w):
        body = Panel(bg=BG_BODY, padding=(0, dp(4), 0, dp(4)))
        body.add_widget(WrapLabel(
            text=f"[color={GREY}]{esc(w.address)}[/color]", markup=True, font_size=sp(10), padding=(dp(12), 0),
        ))
        if w.error:
            body.add_widget(WrapLabel(
                text=f"[color={RED}]Erreur : {esc(w.error)}[/color]", markup=True, font_size=sp(12), padding=(dp(12), 0),
            ))
            return body
        if w.warning:
            body.add_widget(WrapLabel(
                text=f"[color={ORANGE}]{esc(w.warning)}[/color]", markup=True, font_size=sp(11), padding=(dp(12), 0),
            ))

        shown = 0
        if not w.native_hidden:
            body.add_widget(self._row(
                w.native_symbol, "coin", None, w.native_amount, w.native_usd_value, w.native_eur_value,
            ))
            shown += 1
        for t in sorted(w.tokens, key=lambda t: -(t.usd_value or 0.0)):
            tag = t.asset_type if t.asset_type != "token" else None
            name = t.name if t.name and t.name != t.symbol else None
            body.add_widget(self._row(t.symbol, tag, name, t.amount, t.usd_value, t.eur_value))
            shown += 1

        hidden = w.hidden_unpriced_count + w.hidden_dust_count
        if hidden:
            body.add_widget(WrapLabel(
                text=f"[color={GREY}]{hidden} position(s) masquee(s) (sans valeur ou < 1 centime)[/color]",
                markup=True, font_size=sp(11), padding=(dp(12), 0),
            ))
        elif not shown:
            body.add_widget(WrapLabel(
                text=f"[color={GREY}]Aucune position.[/color]", markup=True, font_size=sp(12), padding=(dp(12), 0),
            ))
        return body

    @staticmethod
    def _row(symbol, tag, name, amount, usd, eur):
        left = f"[b]{esc(symbol)}[/b]"
        if tag:
            left += f"  [color={GREY}]{esc(tag)}[/color]"
        left += f"\n{fmt_amount(amount)}"
        if name:
            left += f"\n[size={int(sp(11))}][color={GREY}]{esc(name)}[/color][/size]"
        right = f"[b]{fmt_usd(usd)}[/b]\n[color={GREY}]{fmt_eur(eur)}[/color]"
        return Row(left, right)

    # ---------------------------------------------------------------- export

    def copy_text(self, _instance):
        if not self.last_text:
            return
        Clipboard.copy(self.last_text)
        self.status_label.text = "Texte copie dans le presse-papier."

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
