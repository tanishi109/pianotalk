"""Menu bar icon for the recorder daemon (AppKit via pyobjc).

The daemon's work runs from an NSTimer on the main run loop in *common* modes,
so recording keeps working while the menu is open. The timer runs fast only
while a take is being recorded (Daemon.interval()).

Icon states:  pianokeys (gray)  waiting for the piano
              pianokeys          connected, idle
              pianokeys + red ●  recording
              pianokeys + red ○  all keys/pedal up, saving in a few seconds
              pianokeys + ✓ / –  just saved / just discarded as too short (3 s)
              speaker            Claude is playing (input muted)
              pause              paused by the user
"""

from __future__ import annotations

import logging
import time

import objc
from AppKit import (

    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBeep,
    NSColor,
    NSEvent,
    NSEventTypeApplicationDefined,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageLeft,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSTerminateLater,
    NSVariableStatusItemLength,
    NSWorkspace,
)
from Foundation import NSURL, NSMakePoint, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

from . import config, loginitem, storage
from .midiutil import analyze

log = logging.getLogger("pianotalk.menubar")

FLASH_SECONDS = 3.0  # how long the ✓ / – after a take stays in the menu bar
MIDI_SETUP_BUNDLE = "com.apple.audio.AudioMIDISetup"
REPLAY_RECORDINGS = 10   # how many of each kind the 聴き返す submenu lists
REPLAY_SUGGESTIONS = 5
NOTICE_SECONDS = 10.0    # how long a "could not replay" notice stays in the menu

_info_cache: dict[tuple[str, float], str] = {}


def _file_label(p) -> str:
    """'11/17 14:01・42音・14.3秒' (cached per file version)."""
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return p.stem
    if key not in _info_cache:
        when = storage.recorded_at(p)
        try:
            rec = analyze(p)
            info = f"・{len(rec.notes)}音・{rec.duration:.1f}秒"
        except Exception:
            info = "・読み込めません"
        _info_cache[key] = f"{when:%m/%d %H:%M}{info}"
    return _info_cache[key]


def _symbol(name: str, desc: str):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, desc)
    if img is not None:
        img.setTemplate_(True)
    return img


class _Target(NSObject, protocols=[objc.protocolNamed("NSMenuDelegate"),
                                   objc.protocolNamed("NSApplicationDelegate")]):
    """Objective-C target for the timer and menu actions."""

    def initWithUI_(self, ui):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self.ui = ui
        return self

    def tick_(self, _timer):
        self.ui.tick()

    def togglePause_(self, _sender):
        self.ui.daemon.set_paused(not self.ui.daemon.paused)
        self.ui.refresh(force=True)

    def revealLast_(self, _sender):
        last = self.ui.daemon.rec.last_saved
        if last and last[0].exists():
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(str(last[0]))])
        else:
            self.openRecordings_(None)

    def openRecordings_(self, _sender):
        d = config.recordings_dir()
        d.mkdir(parents=True, exist_ok=True)
        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(d)))

    def openLog_(self, _sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(config.logs_dir() / "recorder.log")))

    def menuNeedsUpdate_(self, menu):
        if menu is self.ui.replay_menu:
            self.ui.fill_replay_menu()
        elif menu is self.ui.menu:
            self.ui.update_login_item()  # may have been changed in System Settings

    def toggleLoginItem_(self, _sender):
        self.ui.toggle_login_item()

    def replay_(self, sender):
        from pathlib import Path

        self.ui.start_replay(Path(sender.representedObject()))

    def stopReplay_(self, _sender):
        self.ui.daemon.stop_replay()
        self.ui.refresh(force=True)

    def openMidiSetup_(self, _sender):
        ws = NSWorkspace.sharedWorkspace()
        url = ws.URLForApplicationWithBundleIdentifier_(MIDI_SETUP_BUNDLE)
        if url is not None:
            ws.openURL_(url)

    def quit_(self, _sender):
        log.info("quit requested from menu bar")
        self.ui.daemon.running = False

    def applicationShouldTerminate_(self, _app):
        # Quit from outside (⌘Q via AppleScript, logout, shutdown): finish the
        # current take first, then let AppKit terminate (see MenuBar.tick).
        log.info("quit requested by the system")
        self.ui.terminate_requested = True
        self.ui.daemon.running = False
        return NSTerminateLater


class MenuBar:
    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.app = NSApplication.sharedApplication()
        self.terminate_requested = False
        self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
        self.target = _Target.alloc().initWithUI_(self)
        self.app.setDelegate_(self.target)
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item.setAutosaveName_("PianoTalkRecorder")
        self.item.button().setImagePosition_(NSImageLeft)
        self.icons = {
            "piano": _symbol("pianokeys", "PianoTalk"),
            "playing": _symbol("speaker.wave.2", "PianoTalk 再生中"),
            "paused": _symbol("pause.circle", "PianoTalk 一時停止中"),
        }
        self._build_menu()
        self._last_key = None
        self.notice: tuple[str, float] | None = None
        self.refresh(force=True)
        log.info("menu bar icon shown")

    def _add(self, title: str, action: str | None = None) -> NSMenuItem:
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        if action:
            it.setTarget_(self.target)
        else:
            it.setEnabled_(False)
        self.menu.addItem_(it)
        return it

    def _build_menu(self) -> None:
        self.menu = NSMenu.alloc().init()
        self.menu.setAutoenablesItems_(False)
        self._add("PianoTalk 録音")
        self.status_item = self._add("")
        self.result_item = self._add("")  # "too short, not saved" notice
        self.last_item = self._add("", "revealLast:")
        self.midi_item = self._add("Audio MIDI 設定を開く…", "openMidiSetup:")
        self.midi_hint = self._add("　MIDIスタジオ（⌘2）の Bluetooth からピアノを接続")
        self.notice_item = self._add("")  # "could not replay: …"
        self.menu.addItem_(NSMenuItem.separatorItem())
        self.stop_item = self._add("■ 再生を停止", "stopReplay:")
        replay_root = self._add("ピアノで聴き返す", None)
        replay_root.setEnabled_(True)
        self.replay_menu = NSMenu.alloc().init()
        self.replay_menu.setAutoenablesItems_(False)
        self.replay_menu.setDelegate_(self.target)
        replay_root.setSubmenu_(self.replay_menu)
        self.pause_item = self._add("録音を一時停止", "togglePause:")
        self._add("録音フォルダを開く", "openRecordings:")
        self._add("ログを開く", "openLog:")
        self.menu.addItem_(NSMenuItem.separatorItem())
        self.login_item = self._add("ログイン時に起動", "toggleLoginItem:")
        self.login_item.setHidden_(loginitem.status() == loginitem.UNAVAILABLE)
        self._add("終了", "quit:")
        self.menu.setDelegate_(self.target)
        self.item.setMenu_(self.menu)
        self.update_login_item()

    def update_login_item(self) -> None:
        st = loginitem.status()
        self.login_item.setHidden_(st == loginitem.UNAVAILABLE)
        self.login_item.setState_(1 if st in (loginitem.ENABLED, loginitem.NEEDS_APPROVAL) else 0)
        self.login_item.setTitle_("ログイン時に起動（システム設定で許可が必要）" if st == loginitem.NEEDS_APPROVAL
                                  else "ログイン時に起動")

    def toggle_login_item(self) -> None:
        st = loginitem.status()
        if st == loginitem.NEEDS_APPROVAL:
            loginitem.open_settings()  # the user has to allow it there
            return
        err = loginitem.set_enabled(st != loginitem.ENABLED)
        if err:
            self.notice = (err, time.monotonic())
        elif loginitem.status() == loginitem.NEEDS_APPROVAL:
            loginitem.open_settings()
        self.update_login_item()
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        st = self.daemon.status()
        res = st["last_result"]
        flash = None
        if res and not st["recording"] and time.monotonic() - res["at"] < FLASH_SECONDS:
            flash = res["kind"]
        key = (st["port"], st["recording"], st["elapsed"], st["notes"], st["playing"],
               st["paused"], st.get("ambiguous"), st["last_saved"], st["until_save"], st["will_save"],
               id(res), flash, str(st["replay"]), st["replay_tail"], self._notice_text())
        if key == self._last_key and not force:
            return
        self._last_key = key
        btn = self.item.button()

        rp = st["replay"]
        if rp:
            e, t = divmod(rp["elapsed"], 60), divmod(rp["total"], 60)
            kind = "フレーズ" if rp["path"].parent == config.suggestions_dir() else "録音"
            icon, status = "playing", (f"▶ 聴き返し中 {e[0]}:{e[1]:02d} / {t[0]}:{t[1]:02d}"
                                      f" — {storage.recorded_at(rp['path']):%m/%d %H:%M} の{kind}")
        elif st["replay_tail"]:
            icon, status = "playing", "聴き返しを終了しました"
        elif st["paused"]:
            icon, status = "paused", "一時停止中（録音しません）"
        elif st["playing"]:
            icon, status = "playing", "Claude が再生中（入力を無視）"
        elif st["recording"]:
            m, s = divmod(st["elapsed"], 60)
            took = f"{m}:{s:02d}・{st['notes']}音"
            if st["until_save"] is None:
                icon, status = "piano", f"● 録音中 {took}"
            elif st["will_save"]:
                icon, status = "piano", f"○ まもなく保存（あと{st['until_save']}秒）— {took}"
            else:
                icon, status = "piano", f"○ あと{st['until_save']}秒で終了・短いため保存しません — {took}"
        elif st["port"]:
            icon, status = "piano", f"待機中: {st['port']}"
        elif st.get("ambiguous"):
            icon, status = "piano", "MIDI機器が複数あります — config.toml の input_port で指定してください"
        else:
            icon, status = "piano", f"ピアノ未接続 — {st['wanted']}を探しています"

        img = self.icons.get(icon)
        if img is not None:
            btn.setImage_(img)
        badge = None
        if not st["paused"] and not st["playing"]:
            if st["recording"]:
                badge = (" ●" if st["until_save"] is None else " ○", NSColor.systemRedColor())
            elif flash == "saved":
                badge = (" ✓", NSColor.systemGreenColor())
            elif flash == "discarded":
                badge = (" –", NSColor.secondaryLabelColor())
        if badge:
            attrs = {NSForegroundColorAttributeName: badge[1],
                     NSFontAttributeName: NSFont.menuBarFontOfSize_(0)}
            btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(badge[0], attrs))
        else:
            btn.setTitle_("" if img is not None else "♪")
        btn.setAppearsDisabled_(st["port"] is None and not st["paused"])
        tip = status
        if flash == "saved":
            tip = f"保存しました（{res['notes']}音・{res['seconds']:.1f}秒）"
        elif flash == "discarded":
            tip = "短いため保存しませんでした"
        btn.setToolTip_(f"PianoTalk — {tip}")

        self.status_item.setTitle_(status)
        if res and res["kind"] == "discarded":
            self.result_item.setTitle_(
                f"直前の演奏は短いため保存しませんでした（{res['notes']}音・{res['seconds']:.1f}秒。"
                f"{st['min_notes']}音以上かつ{st['min_seconds']:g}秒以上で保存）")
            self.result_item.setHidden_(False)
        else:
            self.result_item.setHidden_(True)
        last = st["last_saved"]
        if last:
            path, n, dur = last
            when = storage.recorded_at(path)
            detail = f"・{n}音・{dur:.1f}秒" if n else ""
            self.last_item.setTitle_(f"最後の録音: {when:%m/%d %H:%M}{detail} — Finder で表示")
            self.last_item.setEnabled_(True)
        else:
            self.last_item.setTitle_("最後の録音: まだありません")
            self.last_item.setEnabled_(False)
        unplugged = st["port"] is None
        self.midi_item.setHidden_(not unplugged)
        self.midi_hint.setHidden_(not unplugged)
        self.stop_item.setHidden_(rp is None)
        notice = self._notice_text()
        self.notice_item.setTitle_(notice or "")
        self.notice_item.setHidden_(not notice)
        self.pause_item.setTitle_("録音を再開" if st["paused"] else "録音を一時停止")

    def _notice_text(self) -> str | None:
        if self.notice and time.monotonic() - self.notice[1] < NOTICE_SECONDS:
            return self.notice[0]
        return None

    def start_replay(self, path) -> None:
        why = self.daemon.replay(path)
        if why:
            self.notice = (f"再生できませんでした: {why}", time.monotonic())
            NSBeep()
        else:
            self.notice = None
        self.refresh(force=True)

    def fill_replay_menu(self) -> None:
        m = self.replay_menu
        m.removeAllItems()
        st = self.daemon.status()
        blocked = None
        if st["port"] is None:
            blocked = "ピアノ未接続のため再生できません"
        elif st["recording"]:
            blocked = "録音中は再生できません"

        def add(title, path=None):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "replay:" if path else None, "")
            if path:
                it.setTarget_(self.target)
                it.setRepresentedObject_(str(path))
                it.setEnabled_(blocked is None)
            else:
                it.setEnabled_(False)
            m.addItem_(it)

        if blocked:
            add(blocked)
            m.addItem_(NSMenuItem.separatorItem())
        add("自分の演奏")
        recs = storage.all_recordings()[:REPLAY_RECORDINGS]
        for p in recs:
            add(_file_label(p) + ("  [保存印]" if storage.is_kept(p) else ""), p)
        if not recs:
            add("　まだありません")
        m.addItem_(NSMenuItem.separatorItem())
        add("Claude のフレーズ")
        sugs = storage.all_suggestions()[:REPLAY_SUGGESTIONS]
        for p in sugs:
            add(_file_label(p), p)
        if not sugs:
            add("　まだありません")
        m.addItem_(NSMenuItem.separatorItem())
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("録音フォルダを開く", "openRecordings:", "")
        it.setTarget_(self.target)
        m.addItem_(it)

    def tick(self) -> None:
        try:
            self.daemon.step()
        except Exception:
            log.exception("error in main loop")
        # checked even when step() failed, so a recurring error can't block quitting
        if not self.daemon.running:
            if self.terminate_requested:
                self.timer.invalidate()
                NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
                self.daemon.shutdown()
                self.app.replyToApplicationShouldTerminate_(True)  # AppKit exits the process
                return
            self._stop()
            return
        try:
            self.refresh()
            if self.daemon.interval() != self.timer_interval:
                self._schedule(self.daemon.interval())
        except Exception:
            log.exception("error in main loop")

    def _schedule(self, interval: float) -> None:
        old = getattr(self, "timer", None)
        if old is not None:
            old.invalidate()
        self.timer_interval = interval
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self.target, "tick:", None, True)
        self.timer.setTolerance_(interval * 0.2)  # lets macOS coalesce wakeups
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def run(self) -> None:
        self._schedule(self.daemon.interval())
        self.app.run()

    def _stop(self) -> None:
        self.timer.invalidate()
        NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
        self.app.stop_(None)
        # stop_ takes effect after the next event; post a dummy one
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            NSEventTypeApplicationDefined, NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0)
        self.app.postEvent_atStart_(ev, True)
