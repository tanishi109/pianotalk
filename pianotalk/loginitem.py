"""Open at login" for PianoTalk Recorder.app via SMAppService (macOS 13+).

The app registers itself as a login item. It then shows up, and can be turned
off, in System Settings → General → Login Items, so nothing starts at login
without the user being able to see it there. Only meaningful inside the .app
bundle (py2app sets sys.frozen); from a terminal the menu item is hidden.
"""

from __future__ import annotations

import logging

log = logging.getLogger("pianotalk.loginitem")

ENABLED, OFF, NEEDS_APPROVAL, UNAVAILABLE = "enabled", "off", "needs_approval", "unavailable"


def _service():
    try:
        from ServiceManagement import SMAppService
    except ImportError:
        return None
    return SMAppService.mainAppService()


def status() -> str:
    import sys

    import ServiceManagement as SM

    s = _service()
    if s is None or not getattr(sys, "frozen", False):  # only the .app can register itself
        return UNAVAILABLE
    st = s.status()
    if st == SM.SMAppServiceStatusEnabled:
        return ENABLED
    if st == SM.SMAppServiceStatusRequiresApproval:
        return NEEDS_APPROVAL
    # NotRegistered, or NotFound: what macOS reports for an app that has never
    # registered (seen on macOS 27 with the ad-hoc signed app). Either way: off.
    return OFF


def set_enabled(on: bool) -> str | None:
    """Register or unregister. Returns an error message to show, or None."""
    s = _service()
    if s is None:
        return "この macOS ではログイン時の起動を設定できません"
    if on:
        ok, err = s.registerAndReturnError_(None)
    else:
        ok, err = s.unregisterAndReturnError_(None)
    log.info("login item %s: ok=%s status=%s err=%s", "register" if on else "unregister", ok, status(), err)
    if not ok:
        return f"ログイン時の起動を{'オン' if on else 'オフ'}にできませんでした（{err.localizedDescription() if err else '不明なエラー'}）"
    return None


def open_settings() -> None:
    s = _service()
    if s is not None:
        from ServiceManagement import SMAppService

        SMAppService.openSystemSettingsLoginItems()
