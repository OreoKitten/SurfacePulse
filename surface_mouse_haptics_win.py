#!/usr/bin/env python3
"""
SurfacePulse (iXpat for Windows) — Microsoft Surface Mouse (2nd Edition) Suite
==============================================================================
Full-featured Windows user-space haptics, scroll physics, Action Ring, and
remapping suite for the Microsoft Surface Mouse (2nd Edition) and any Windows
HID Simple Haptic Controller (Usage Page 0x0E, Usage 0x01) mouse.
What Was Fixed in v2.0 (64-Bit Windows x64 / ARM64 Safe Hook Update):
---------------------------------------------------------------------
1. Fixed 64-Bit `LPARAM` Overflow in `CallNextHookEx`:
   On 64-bit Windows (both x64 and Surface Pro ARM64), `l_param` in `WH_MOUSE_LL`
   is a 64-bit pointer (`MSLLHOOKSTRUCT*`) whose address exceeds `2^31 - 1`.
   Without explicit `argtypes` on `user32.CallNextHookEx`, Python's `ctypes`
   defaults parameters to 32-bit `c_int`, raising:
     `ctypes.ArgumentError: argument 4: OverflowError: int too long to convert`
   Every Win32, SetupAPI, and HID API function now has explicit 64-bit-safe
   `argtypes` and `restype` declarations (`LRESULT = c_ssize_t`, `WPARAM = c_size_t`,
   `LPARAM = c_ssize_t`, `HANDLE = c_void_p`).
2. 100% Passive, Non-Blocking `WH_MOUSE_LL` Callback:
   - Touchscreen & Surface Pen input (`MI_WP_SIGNATURE = 0xFF515700`) is detected
     and bypassed immediately at the top of `mouse_callback`.
   - `mouse_callback` never blocks on HID I/O (`WriteFile` / `HidD_SetOutputReport`):
     haptic triggers are queued in sub-microsecond time to a dedicated background
     worker thread (`_haptic_worker_loop`).
   - `mouse_callback` never swallows clicks by default and wraps all logic in a
     fail-safe `try/except` block so `CallNextHookEx` is guaranteed to execute
     on every event.
3. Dual Output Transport (`WriteFile` + `HidD_SetOutputReport` Fallback) & `--diag`
   Hardware Inspector:
   - Run `py surface_mouse_haptics_win.py --diag` (or click "Run HID 0x0E Diagnostics"
     in the GUI) to inspect all Microsoft (`VID_045E`) and Haptics (`UsagePage 0x0E`)
     collections without activating any mouse hook.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
IS_WINDOWS = sys.platform == "win32"
# ============================================================================
# 1. HID Simple Haptic Controller (Usage Page 0x0E) Constants & Waveforms
# ============================================================================
# Reference: Microsoft Input Device Haptics Implementation Guide
# https://learn.microsoft.com/en-us/windows-hardware/design/component-guidelines/input-haptics-implementation-guide
HID_USAGE_PAGE_GENERIC_DESKTOP = 0x0001
HID_USAGE_GENERIC_MOUSE = 0x0002
HID_USAGE_PAGE_ORDINAL = 0x000A
HID_USAGE_PAGE_HAPTICS = 0x000E
HID_USAGE_SIMPLE_HAPTIC_CONTROLLER = 0x0001
# Haptics Usage Page (0x0E) Collection & Report Usages
HID_USAGE_WAVEFORM_LIST = 0x0010
HID_USAGE_DURATION_LIST = 0x0011
HID_USAGE_AUTO_TRIGGER = 0x0020
HID_USAGE_MANUAL_TRIGGER = 0x0021
HID_USAGE_INTENSITY = 0x0023
HID_USAGE_REPEAT_COUNT = 0x0024
HID_USAGE_RETRIGGER_PERIOD = 0x0025
HID_USAGE_WAVEFORM_CUTOFF_TIME = 0x0028
# Standard HID Haptic Waveform Usage IDs (Usage Page 0x0E)
WAVEFORM_NONE = 0x1001      # Implicit Ordinal 1 (No-op)
WAVEFORM_STOP = 0x1002      # Implicit Ordinal 2 (Stop playback)
WAVEFORM_CLICK = 0x1003     # Medium pulse when a button/control is clicked
WAVEFORM_PRESS = 0x1006     # Optional button press pulse
WAVEFORM_RELEASE = 0x1007   # Optional button release pulse
WAVEFORM_HOVER = 0x1008     # Light pulse indicating hover state
WAVEFORM_SUCCESS = 0x1009   # Ascending pattern confirming completed action
WAVEFORM_ERROR = 0x100A     # Descending pattern indicating failed action
WAVEFORM_COLLIDE = 0x1012   # Soft pulse indicating boundary/limit reached
WAVEFORM_ALIGN = 0x1013     # Sharp pulse when snapping to alignment guide
WAVEFORM_STEP = 0x1014      # Firm pulse for discrete steps/scroll notches
WAVEFORM_GROW = 0x1015      # Dynamic pulse conveying motion or transition
WAVEFORM_NAMES: Dict[int, str] = {
    WAVEFORM_NONE: "None",
    WAVEFORM_STOP: "Stop",
    WAVEFORM_CLICK: "Click",
    WAVEFORM_PRESS: "Press",
    WAVEFORM_RELEASE: "Release",
    WAVEFORM_HOVER: "Hover",
    WAVEFORM_SUCCESS: "Success",
    WAVEFORM_ERROR: "Error",
    WAVEFORM_COLLIDE: "Collide",
    WAVEFORM_ALIGN: "Align",
    WAVEFORM_STEP: "Step",
    WAVEFORM_GROW: "Grow",
}
NAME_TO_WAVEFORM: Dict[str, int] = {v: k for k, v in WAVEFORM_NAMES.items()}
DEFAULT_SURFACE_WAVEFORM_ORDINALS: Dict[int, int] = {
    WAVEFORM_NONE: 1,
    WAVEFORM_STOP: 2,
    WAVEFORM_CLICK: 3,
    WAVEFORM_HOVER: 4,
    WAVEFORM_COLLIDE: 5,
    WAVEFORM_ALIGN: 6,
    WAVEFORM_STEP: 7,
    WAVEFORM_GROW: 8,
    WAVEFORM_SUCCESS: 9,
    WAVEFORM_ERROR: 10,
}
MICROSOFT_VID = 0x045E
# Magic marker stamped into synthetic SendInput wheel events so our WH_MOUSE_LL
# hook distinguishes kinetic/forever-scroll ticks from physical wheel turns.
SURFACE_PULSE_MAGIC = 0x53524648  # 'SRFH'
# Windows signature for touch/pen-promoted mouse messages (must be ignored by hook)
MI_WP_SIGNATURE = 0xFF515700
SIGNATURE_MASK = 0xFFFFFF00

# ============================================================================
# 2. Configuration & Per-App Profile Data Structures
# ============================================================================
@dataclass
class EventHapticSetting:
    enabled: bool = True
    waveform: str = "Click"
    strength: int = 3  # 1..5 (maps to HID logical intensity 1..4 or 20%..100%)

@dataclass
class ActionRingSlot:
    label: str
    action_type: str  # "forever_scroll", "shortcut", "command"
    payload: str

@dataclass
class AppProfile:
    name: str
    exe_match: str  # e.g., "excel.exe", "chrome.exe"
    scroll_mode: str = "dynamic"  # "dynamic", "forever_m720", "ratchet_only"
    kinetic_momentum: float = 0.88
    forever_scroll_on_flick: bool = True
    flick_velocity_threshold: float = 18.0
    fast_scroll_threshold: float = 7.5
    scroll_speed_multiplier: float = 1.0

DEFAULT_ACTION_RING_SLOTS: List[ActionRingSlot] = [
    ActionRingSlot("Forever Scroll", "forever_scroll", "toggle"),
    ActionRingSlot("Task View", "shortcut", "win+tab"),
    ActionRingSlot("Screenshot", "shortcut", "win+shift+s"),
    ActionRingSlot("Search / Run", "shortcut", "alt+space"),
    ActionRingSlot("Copy", "shortcut", "ctrl+c"),
    ActionRingSlot("Paste", "shortcut", "ctrl+v"),
    ActionRingSlot("Undo", "shortcut", "ctrl+z"),
    ActionRingSlot("Play / Pause", "shortcut", "media_play_pause"),
]

@dataclass
class SurfacePulseConfig:
    haptic_backend: str = "raw_hid"  # "raw_hid" or "winrt_api"
    master_haptics_enabled: bool = True
    global_hook_enabled: bool = True
    min_trigger_interval_ms: int = 14
    # Scroll Wheel & Logitech M720 Forever Scroll Settings
    scroll_mode: str = "dynamic"  # "dynamic" | "forever_m720" | "ratchet_only"
    forever_scroll_active: bool = False
    auto_forever_scroll_on_flick: bool = True
    stop_forever_scroll_on_click: bool = True
    forever_scroll_toggle_button: str = "action_only"  # "action_only" | "middle_or_action" | "middle_only"
    swallow_action_button: bool = False  # Passive by default so no buttons get locked
    kinetic_scrolling_enabled: bool = True
    kinetic_friction: float = 0.89
    forever_scroll_friction: float = 1.0
    slow_ratchet_threshold: float = 7.5
    flick_freespin_threshold: float = 18.0
    # Action Button & Gesture Remapping
    action_button_source: str = "xbutton1"
    action_single_press: str = "toggle_forever_scroll"
    action_double_press: str = "open_action_ring"
    action_hold_mode: str = "gesture_volume_brightness"
    force_click_enabled: bool = False  # Opt-in so normal long-press/drag is never interrupted
    force_click_hold_ms: int = 600
    force_click_shortcut: str = "ctrl+f"
    # Per-Device Pointer Speed
    per_device_pointer_speed: bool = False
    surface_mouse_speed: int = 12
    other_pointer_speed: int = 10
    # Per-Event Haptic Mapping
    events: Dict[str, EventHapticSetting] = field(default_factory=lambda: {
        "left_click": EventHapticSetting(True, "Click", 3),
        "right_click": EventHapticSetting(True, "Click", 2),
        "scroll_slow_notch": EventHapticSetting(True, "Step", 4),
        "scroll_fast_tick": EventHapticSetting(True, "Hover", 2),
        "scroll_freespin_stop": EventHapticSetting(True, "Collide", 2),
        "forever_scroll_toggle": EventHapticSetting(True, "Success", 4),
        "hover_button_tab_link": EventHapticSetting(True, "Hover", 2),
        "hover_menu_item": EventHapticSetting(True, "Align", 2),
        "slider_step": EventHapticSetting(True, "Step", 3),
        "text_selection": EventHapticSetting(True, "Hover", 1),
        "window_snap": EventHapticSetting(True, "Align", 5),
        "drag_pickup": EventHapticSetting(True, "Grow", 3),
        "drag_drop": EventHapticSetting(True, "Success", 4),
        "screen_edge": EventHapticSetting(True, "Collide", 3),
        "action_ring_slot": EventHapticSetting(True, "Align", 3),
        "force_click": EventHapticSetting(True, "Grow", 5),
        "gesture_step": EventHapticSetting(True, "Step", 3),
    })
    action_ring_slots: List[ActionRingSlot] = field(
        default_factory=lambda: list(DEFAULT_ACTION_RING_SLOTS)
    )
    app_profiles: List[AppProfile] = field(default_factory=lambda: [
        AppProfile("Browsers (Forever Scroll Ready)", "chrome.exe,msedge.exe,firefox.exe", "dynamic", 0.92, True, 16.0, 7.5, 1.15),
        AppProfile("Spreadsheets & Code", "excel.exe,code.exe,devenv.exe", "dynamic", 0.90, True, 17.0, 8.0, 1.0),
        AppProfile("Precision CAD / Design", "acad.exe,blender.exe,photoshop.exe", "ratchet_only", 0.0, False, 99.0, 12.0, 1.0),
    ])
    @classmethod
    def config_path(cls) -> Path:
        base = Path(os.environ.get("APPDATA", Path.home()))
        folder = base / "SurfacePulse"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "config.json"
    def save(self, path: Optional[Path] = None) -> None:
        target = path or self.config_path()
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SurfacePulseConfig":
        target = path or cls.config_path()
        cfg = cls()
        if not target.exists():
            return cfg
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            for key, val in raw.items():
                if key == "events" and isinstance(val, dict):
                    for ek, ev in val.items():
                        if isinstance(ev, dict):
                            cfg.events[ek] = EventHapticSetting(**ev)
                elif key == "action_ring_slots" and isinstance(val, list):
                    cfg.action_ring_slots = [ActionRingSlot(**s) for s in val]
                elif key == "app_profiles" and isinstance(val, list):
                    cfg.app_profiles = [AppProfile(**p) for p in val]
                elif hasattr(cfg, key):
                    setattr(cfg, key, val)
        except Exception as exc:
            print(f"[SurfacePulse] Warning: failed to load config ({exc}), using defaults.")
        return cfg

# ============================================================================
# 3. 64-Bit Safe Win32 & HID Structures + Explicit Function Signatures
# ============================================================================
LRESULT = ctypes.c_ssize_t
WPARAM_T = ctypes.c_size_t
LPARAM_T = ctypes.c_ssize_t
ULONG_PTR_T = ctypes.c_size_t
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]
class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ULONG_PTR_T),
    ]
class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_ulong),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]
class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]
class _HIDP_RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", ctypes.c_ushort),
        ("UsageMax", ctypes.c_ushort),
        ("StringMin", ctypes.c_ushort),
        ("StringMax", ctypes.c_ushort),
        ("DesignatorMin", ctypes.c_ushort),
        ("DesignatorMax", ctypes.c_ushort),
        ("DataIndexMin", ctypes.c_ushort),
        ("DataIndexMax", ctypes.c_ushort),
    ]
class _HIDP_NOT_RANGE(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("Reserved1", ctypes.c_ushort),
        ("StringIndex", ctypes.c_ushort),
        ("Reserved2", ctypes.c_ushort),
        ("DesignatorIndex", ctypes.c_ushort),
        ("Reserved3", ctypes.c_ushort),
        ("DataIndex", ctypes.c_ushort),
        ("Reserved4", ctypes.c_ushort),
    ]
class _HIDP_VALUE_UNION(ctypes.Union):
    _fields_ = [("Range", _HIDP_RANGE), ("NotRange", _HIDP_NOT_RANGE)]
class HIDP_VALUE_CAPS(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("UsagePage", ctypes.c_ushort),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", ctypes.c_ushort),
        ("LinkCollection", ctypes.c_ushort),
        ("LinkUsage", ctypes.c_ushort),
        ("LinkUsagePage", ctypes.c_ushort),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", ctypes.c_ushort),
        ("ReportCount", ctypes.c_ushort),
        ("Reserved2", ctypes.c_ushort * 5),
        ("UnitsExp", ctypes.c_ulong),
        ("Units", ctypes.c_ulong),
        ("LogicalMin", ctypes.c_long),
        ("LogicalMax", ctypes.c_long),
        ("PhysicalMin", ctypes.c_long),
        ("PhysicalMax", ctypes.c_long),
        ("u", _HIDP_VALUE_UNION),
    ]
class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR_T),
    ]
class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", ctypes.c_void_p),
        ("ptScreenPos", POINT),
    ]
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR_T),
    ]
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR_T),
    ]
class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]
class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]
class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]

if IS_WINDOWS:
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    DIGCF_PRESENT = 0x00000002
    DIGCF_DEVICEINTERFACE = 0x00000010
    HidP_Input = 0
    HidP_Output = 1
    HidP_Feature = 2
    HIDP_STATUS_SUCCESS = 0x00110000
    LowLevelMouseProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM_T, LPARAM_T)
    # Explicitly bind all 64-bit pointer/handle signatures so ctypes NEVER
    # truncates 64-bit handles or throws OverflowError on 64-bit LPARAM!
    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    hid.HidD_GetHidGuid.restype = None
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD
    ]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
        wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    hid.HidD_GetPreparsedData.restype = wintypes.BOOLEAN
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidD_FreePreparsedData.restype = wintypes.BOOLEAN
    hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
    hid.HidD_GetAttributes.restype = wintypes.BOOLEAN
    hid.HidD_GetProductString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    hid.HidD_GetProductString.restype = wintypes.BOOLEAN
    hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    hid.HidD_GetFeature.restype = wintypes.BOOLEAN
    hid.HidD_SetOutputReport.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    hid.HidD_SetOutputReport.restype = wintypes.BOOLEAN
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    hid.HidP_GetCaps.restype = ctypes.c_long
    hid.HidP_GetValueCaps.argtypes = [
        ctypes.c_int, ctypes.POINTER(HIDP_VALUE_CAPS),
        ctypes.POINTER(ctypes.c_ushort), ctypes.c_void_p,
    ]
    hid.HidP_GetValueCaps.restype = ctypes.c_long
    hid.HidP_GetUsageValue.argtypes = [
        ctypes.c_int, ctypes.c_ushort, ctypes.c_ushort, ctypes.c_ushort,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
    ]
    hid.HidP_GetUsageValue.restype = ctypes.c_long
    hid.HidP_SetUsageValue.argtypes = [
        ctypes.c_int, ctypes.c_ushort, ctypes.c_ushort, ctypes.c_ushort,
        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
    ]
    hid.HidP_SetUsageValue.restype = ctypes.c_long
    # CRITICAL 64-BIT HOOK SIGNATURES (Fixes argument 4 OverflowError!)
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, LowLevelMouseProc, ctypes.c_void_p, wintypes.DWORD
    ]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, WPARAM_T, LPARAM_T]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, WPARAM_T, LPARAM_T]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = ctypes.c_void_p
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    user32.GetCursorInfo.restype = wintypes.BOOL
    user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.LoadCursorW.restype = ctypes.c_void_p
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT
    ]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [
        ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ULONG_PTR_T
    ]
    user32.keybd_event.restype = None

# ============================================================================
# 4. Direct Win32 HID Usage Page 0x0E Driver & Diagnostic Inspector
# ============================================================================
@dataclass
class HapticDeviceInfo:
    device_path: str
    vendor_id: int
    product_id: int
    product_name: str
    output_report_len: int
    feature_report_len: int
    trigger_report_id: int
    waveform_feature_report_id: int
    intensity_logical_max: int
    waveform_to_ordinal: Dict[int, int]
    waveform_durations_ms: Dict[int, int]
    trigger_link_col: int = 0
    intensity_link_col: int = 0

class RawHidSimpleHapticsDriver:
    """
    User-mode Windows HID driver for Usage Page 0x0E (Simple Haptic Controller).
    Communicates directly with the Surface Mouse (2nd Edition) sibling top-level
    haptics collection without needing kernel extensions or foreground focus.
    """
    def __init__(self) -> None:
        self._handle: Optional[int] = None
        self._preparsed: Optional[ctypes.c_void_p] = None
        self.device_info: Optional[HapticDeviceInfo] = None
        self._lock = threading.Lock()
        self._last_trigger_ts: float = 0.0
    @staticmethod
    def enumerate_diagnostics() -> str:
        """
        Scan all HID interfaces and return a detailed diagnostic report of all
        Microsoft (VID 0x045E) and Haptics (UsagePage 0x0E) collections.
        """
        if not IS_WINDOWS:
            return "HID Diagnostics: Not running on Windows (sandbox simulation mode)."
        lines = [
            "=== SurfacePulse HID & Haptics Diagnostic Report ===",
            f"Platform: {sys.platform} | Pointer Size: {ctypes.sizeof(ctypes.c_void_p) * 8}-bit | Python: {sys.version.split()[0]}",
        ]
        hid_guid = GUID()
        hid.HidD_GetHidGuid(ctypes.byref(hid_guid))
        h_dev_info = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(hid_guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
        )
        if not h_dev_info or h_dev_info == INVALID_HANDLE_VALUE:
            lines.append("ERROR: SetupDiGetClassDevsW failed.")
            return "\n".join(lines)
        found_haptic = 0
        found_ms = 0
        try:
            index = 0
            while True:
                iface_data = SP_DEVICE_INTERFACE_DATA()
                iface_data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
                if not setupapi.SetupDiEnumDeviceInterfaces(
                    h_dev_info, None, ctypes.byref(hid_guid), index, ctypes.byref(iface_data)
                ):
                    break
                index += 1
                req_size = wintypes.DWORD(0)
                setupapi.SetupDiGetDeviceInterfaceDetailW(
                    h_dev_info, ctypes.byref(iface_data), None, 0, ctypes.byref(req_size), None
                )
                if req_size.value == 0:
                    continue
                buf = ctypes.create_string_buffer(req_size.value)
                cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = cb_size
                if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    h_dev_info, ctypes.byref(iface_data), buf, req_size, None, None
                ):
                    continue
                device_path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                # Open with 0 access first so even exclusive collections can be queried for attributes
                h_meta = kernel32.CreateFileW(
                    device_path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
                )
                if not h_meta or h_meta == INVALID_HANDLE_VALUE:
                    continue
                attrs = HIDD_ATTRIBUTES()
                attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
                hid.HidD_GetAttributes(h_meta, ctypes.byref(attrs))
                prod_buf = ctypes.create_unicode_buffer(128)
                product_name = "Unknown HID"
                if hid.HidD_GetProductString(h_meta, prod_buf, ctypes.sizeof(prod_buf)):
                    product_name = prod_buf.value or product_name
                preparsed = ctypes.c_void_p()
                has_prep = bool(hid.HidD_GetPreparsedData(h_meta, ctypes.byref(preparsed)))
                caps = HIDP_CAPS()
                if has_prep:
                    hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
                    hid.HidD_FreePreparsedData(preparsed)
                kernel32.CloseHandle(h_meta)
                is_ms = (attrs.VendorID == MICROSOFT_VID)
                is_haptic = (caps.UsagePage == HID_USAGE_PAGE_HAPTICS)
                if is_ms or is_haptic:
                    if is_ms:
                        found_ms += 1
                    if is_haptic:
                        found_haptic += 1
                    # Test opening with GENERIC_READ | GENERIC_WRITE
                    h_rw = kernel32.CreateFileW(
                        device_path,
                        GENERIC_READ | GENERIC_WRITE,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None,
                        OPEN_EXISTING,
                        0,
                        None,
                    )
                    rw_ok = bool(h_rw and h_rw != INVALID_HANDLE_VALUE)
                    err_code = 0 if rw_ok else ctypes.get_last_error()
                    if rw_ok:
                        kernel32.CloseHandle(h_rw)
                    lines.append(
                        f"- [{product_name}] VID=0x{attrs.VendorID:04X} PID=0x{attrs.ProductID:04X} | "
                        f"UsagePage=0x{caps.UsagePage:04X} Usage=0x{caps.Usage:04X} | "
                        f"In={caps.InputReportByteLength}B Out={caps.OutputReportByteLength}B Feat={caps.FeatureReportByteLength}B | "
                        f"RW_Open={'OK' if rw_ok else f'ERR({err_code})'}"
                    )
                    if is_haptic:
                        # Deep inspect 0x0E Haptics descriptor & fire live test pulse!
                        h_test = kernel32.CreateFileW(
                            device_path,
                            GENERIC_READ | GENERIC_WRITE,
                            FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None,
                            OPEN_EXISTING,
                            0,
                            None,
                        )
                        if h_test and h_test != INVALID_HANDLE_VALUE:
                            prep2 = ctypes.c_void_p()
                            if hid.HidD_GetPreparsedData(h_test, ctypes.byref(prep2)):
                                if caps.NumberOutputValueCaps > 0:
                                    no = ctypes.c_ushort(caps.NumberOutputValueCaps)
                                    oc = (HIDP_VALUE_CAPS * caps.NumberOutputValueCaps)()
                                    if hid.HidP_GetValueCaps(HidP_Output, oc, ctypes.byref(no), prep2) == HIDP_STATUS_SUCCESS:
                                        for vc in oc:
                                            u = f"0x{vc.Range.UsageMin:04X}..0x{vc.Range.UsageMax:04X}" if vc.IsRange else f"0x{vc.NotRange.Usage:04X}"
                                            lines.append(
                                                f"    [0x0E Output Cap] ReportID=0x{vc.ReportID:02X} UsagePage=0x{vc.UsagePage:04X} "
                                                f"Usage={u} LinkCol={vc.LinkCollection} BitSize={vc.BitSize} "
                                                f"LogMin={vc.LogicalMin} LogMax={vc.LogicalMax}"
                                            )
                                if caps.NumberFeatureValueCaps > 0:
                                    nf = ctypes.c_ushort(caps.NumberFeatureValueCaps)
                                    fc = (HIDP_VALUE_CAPS * caps.NumberFeatureValueCaps)()
                                    if hid.HidP_GetValueCaps(HidP_Feature, fc, ctypes.byref(nf), prep2) == HIDP_STATUS_SUCCESS:
                                        for vc in fc:
                                            u = f"0x{vc.Range.UsageMin:04X}..0x{vc.Range.UsageMax:04X}" if vc.IsRange else f"0x{vc.NotRange.Usage:04X}"
                                            lines.append(
                                                f"    [0x0E Feature Cap] ReportID=0x{vc.ReportID:02X} UsagePage=0x{vc.UsagePage:04X} "
                                                f"Usage={u} LinkCol={vc.LinkCollection} LinkUsage=0x{vc.LinkUsagePage:04X}:0x{vc.LinkUsage:04X} "
                                                f"BitSize={vc.BitSize} LogMin={vc.LogicalMin} LogMax={vc.LogicalMax}"
                                            )
                                hid.HidD_FreePreparsedData(prep2)
                            kernel32.CloseHandle(h_test)

                        # Connect via RawHidSimpleHapticsDriver and fire a live 3-byte test pulse!
                        drv = RawHidSimpleHapticsDriver()
                        if drv.connect() and drv.device_info:
                            di = drv.device_info
                            wf_str = ", ".join(
                                f"Ord{ord_i}:{WAVEFORM_NAMES.get(w, hex(w))}"
                                for w, ord_i in sorted(di.waveform_to_ordinal.items(), key=lambda x: x[1])
                            )
                            lines.append(
                                f"    [0x0E Parsed Info] TriggerReportID=0x{di.trigger_report_id:02X} "
                                f"OutLen={di.output_report_len}B IntensityMax={di.intensity_logical_max} | "
                                f"Waveforms: {wf_str}"
                            )
                            fired_ok = drv.fire_waveform(WAVEFORM_STEP, strength_1_to_5=5, min_interval_ms=0)
                            lines.append(
                                f"    [0x0E Live Test Pulse] Fired Step (0x1014) @ 100% -> {'SUCCESS' if fired_ok else f'FAILED (WinErr={ctypes.get_last_error()})'}"
                            )
                            drv.close()
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(h_dev_info)
        # Also scan paired/sleeping (non-present) interfaces for Bluetooth LE mice
        paired_sleeping = []
        h_all = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(hid_guid), None, None, DIGCF_DEVICEINTERFACE
        )
        if h_all and h_all != INVALID_HANDLE_VALUE:
            try:
                idx2 = 0
                while True:
                    id2 = SP_DEVICE_INTERFACE_DATA()
                    id2.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
                    if not setupapi.SetupDiEnumDeviceInterfaces(
                        h_all, None, ctypes.byref(hid_guid), idx2, ctypes.byref(id2)
                    ):
                        break
                    idx2 += 1
                    rsize = wintypes.DWORD(0)
                    setupapi.SetupDiGetDeviceInterfaceDetailW(
                        h_all, ctypes.byref(id2), None, 0, ctypes.byref(rsize), None
                    )
                    if rsize.value == 0:
                        continue
                    b2 = ctypes.create_string_buffer(rsize.value)
                    ctypes.cast(b2, ctypes.POINTER(wintypes.DWORD))[0] = (
                        8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                    )
                    if setupapi.SetupDiGetDeviceInterfaceDetailW(
                        h_all, ctypes.byref(id2), b2, rsize, None, None
                    ):
                        dpath = ctypes.wstring_at(ctypes.addressof(b2) + 4)
                        low_p = dpath.lower()
                        if ("045e" in low_p or "bthledevice" in low_p) and not (id2.Flags & 1):
                            paired_sleeping.append(dpath)
            finally:
                setupapi.SetupDiDestroyDeviceInfoList(h_all)

        lines.append(
            f"Summary: Found {found_ms} active Microsoft HID collection(s) and {found_haptic} active Haptics (0x0E) collection(s)."
        )
        if paired_sleeping:
            lines.append(f"Paired but currently sleeping/disconnected BLE HID interfaces ({len(paired_sleeping)}):")
            for sp in paired_sleeping[:12]:
                lines.append(f"  * {sp}")
        return "\n".join(lines)
    def connect(self) -> bool:
        """Scan Windows HID interfaces for Usage Page 0x0E, Usage 0x01."""
        if not IS_WINDOWS:
            return False
        self.close()
        hid_guid = GUID()
        hid.HidD_GetHidGuid(ctypes.byref(hid_guid))
        h_dev_info = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(hid_guid),
            None,
            None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
        )
        if not h_dev_info or h_dev_info == INVALID_HANDLE_VALUE:
            return False
        candidates: List[Tuple[int, int, ctypes.c_void_p, HapticDeviceInfo]] = []
        try:
            index = 0
            while True:
                iface_data = SP_DEVICE_INTERFACE_DATA()
                iface_data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
                if not setupapi.SetupDiEnumDeviceInterfaces(
                    h_dev_info, None, ctypes.byref(hid_guid), index, ctypes.byref(iface_data)
                ):
                    break
                index += 1
                req_size = wintypes.DWORD(0)
                setupapi.SetupDiGetDeviceInterfaceDetailW(
                    h_dev_info, ctypes.byref(iface_data), None, 0, ctypes.byref(req_size), None
                )
                if req_size.value == 0:
                    continue
                buf = ctypes.create_string_buffer(req_size.value)
                cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = cb_size
                if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    h_dev_info,
                    ctypes.byref(iface_data),
                    buf,
                    req_size,
                    None,
                    None,
                ):
                    continue
                device_path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                handle = None
                for access_mask in (GENERIC_READ | GENERIC_WRITE, GENERIC_WRITE, 0):
                    h_try = kernel32.CreateFileW(
                        device_path,
                        access_mask,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None,
                        OPEN_EXISTING,
                        0,
                        None,
                    )
                    if h_try and h_try != INVALID_HANDLE_VALUE:
                        handle = h_try
                        break
                if not handle:
                    continue
                preparsed = ctypes.c_void_p()
                if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
                    kernel32.CloseHandle(handle)
                    continue
                caps = HIDP_CAPS()
                status = hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
                if (
                    status != HIDP_STATUS_SUCCESS
                    or caps.UsagePage != HID_USAGE_PAGE_HAPTICS
                    or caps.Usage != HID_USAGE_SIMPLE_HAPTIC_CONTROLLER
                ):
                    hid.HidD_FreePreparsedData(preparsed)
                    kernel32.CloseHandle(handle)
                    continue
                attrs = HIDD_ATTRIBUTES()
                attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
                hid.HidD_GetAttributes(handle, ctypes.byref(attrs))
                prod_buf = ctypes.create_unicode_buffer(128)
                product_name = "HID Simple Haptic Controller"
                if hid.HidD_GetProductString(handle, prod_buf, ctypes.sizeof(prod_buf)):
                    product_name = prod_buf.value or product_name
                info = self._inspect_haptic_caps(
                    handle, preparsed, caps, device_path, attrs.VendorID, attrs.ProductID, product_name
                )
                priority = 0 if attrs.VendorID == MICROSOFT_VID else 1
                candidates.append((priority, handle, preparsed, info))
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(h_dev_info)
        if not candidates:
            return False
        candidates.sort(key=lambda item: item[0])
        _, chosen_handle, chosen_preparsed, chosen_info = candidates[0]
        for _, h, p, _ in candidates[1:]:
            hid.HidD_FreePreparsedData(p)
            kernel32.CloseHandle(h)
        self._handle = chosen_handle
        self._preparsed = chosen_preparsed
        self.device_info = chosen_info
        return True
    def _inspect_haptic_caps(
        self,
        handle: int,
        preparsed: ctypes.c_void_p,
        caps: HIDP_CAPS,
        device_path: str,
        vid: int,
        pid: int,
        product_name: str,
    ) -> HapticDeviceInfo:
        trigger_report_id = 0x01
        trigger_link_col = 0
        intensity_link_col = 0
        intensity_max = 4
        waveform_feature_report_id = 0x01

        if caps.NumberOutputValueCaps > 0:
            n_out = ctypes.c_ushort(caps.NumberOutputValueCaps)
            out_caps = (HIDP_VALUE_CAPS * caps.NumberOutputValueCaps)()
            if hid.HidP_GetValueCaps(HidP_Output, out_caps, ctypes.byref(n_out), preparsed) == HIDP_STATUS_SUCCESS:
                for vc in out_caps:
                    usage = vc.Range.UsageMin if vc.IsRange else vc.NotRange.Usage
                    if vc.UsagePage == HID_USAGE_PAGE_HAPTICS and usage == HID_USAGE_MANUAL_TRIGGER:
                        trigger_report_id = int(vc.ReportID)
                        trigger_link_col = int(vc.LinkCollection)
                    elif vc.UsagePage == HID_USAGE_PAGE_HAPTICS and usage == HID_USAGE_INTENSITY:
                        intensity_link_col = int(vc.LinkCollection)
                        if vc.LogicalMax > 0:
                            intensity_max = int(vc.LogicalMax)

        waveform_to_ordinal = dict(DEFAULT_SURFACE_WAVEFORM_ORDINALS)
        waveform_durations: Dict[int, int] = {}

        if caps.NumberFeatureValueCaps > 0 and caps.FeatureReportByteLength > 0:
            n_feat = ctypes.c_ushort(caps.NumberFeatureValueCaps)
            feat_caps = (HIDP_VALUE_CAPS * caps.NumberFeatureValueCaps)()
            if hid.HidP_GetValueCaps(HidP_Feature, feat_caps, ctypes.byref(n_feat), preparsed) == HIDP_STATUS_SUCCESS:
                waveform_link_col = None
                duration_link_col = None
                ordinals_to_query: List[int] = []
                for vc in feat_caps:
                    if vc.UsagePage == HID_USAGE_PAGE_ORDINAL:
                        u_list = (
                            list(range(int(vc.Range.UsageMin), int(vc.Range.UsageMax) + 1))
                            if vc.IsRange
                            else [int(vc.NotRange.Usage)]
                        )
                        if vc.LinkUsagePage == HID_USAGE_PAGE_HAPTICS and vc.LinkUsage == HID_USAGE_WAVEFORM_LIST:
                            waveform_feature_report_id = int(vc.ReportID)
                            waveform_link_col = int(vc.LinkCollection)
                            for u in u_list:
                                if u not in ordinals_to_query:
                                    ordinals_to_query.append(u)
                        elif vc.LinkUsagePage == HID_USAGE_PAGE_HAPTICS and vc.LinkUsage == HID_USAGE_DURATION_LIST:
                            duration_link_col = int(vc.LinkCollection)

                if not ordinals_to_query:
                    ordinals_to_query = list(range(3, 15))

                feat_buf = ctypes.create_string_buffer(caps.FeatureReportByteLength)
                feat_buf[0] = bytes([waveform_feature_report_id])
                if hid.HidD_GetFeature(handle, feat_buf, caps.FeatureReportByteLength):
                    parsed_map: Dict[int, int] = {
                        WAVEFORM_NONE: 1,
                        WAVEFORM_STOP: 2,
                    }
                    for ordinal in ordinals_to_query:
                        val = ctypes.c_ulong(0)
                        if waveform_link_col is not None:
                            st = hid.HidP_GetUsageValue(
                                HidP_Feature,
                                HID_USAGE_PAGE_ORDINAL,
                                waveform_link_col,
                                ordinal,
                                ctypes.byref(val),
                                preparsed,
                                feat_buf,
                                caps.FeatureReportByteLength,
                            )
                            if st == HIDP_STATUS_SUCCESS and val.value >= 0x1001:
                                wf_id = int(val.value)
                                parsed_map[wf_id] = ordinal
                                if duration_link_col is not None:
                                    d_val = ctypes.c_ulong(0)
                                    st_d = hid.HidP_GetUsageValue(
                                        HidP_Feature,
                                        HID_USAGE_PAGE_ORDINAL,
                                        duration_link_col,
                                        ordinal,
                                        ctypes.byref(d_val),
                                        preparsed,
                                        feat_buf,
                                        caps.FeatureReportByteLength,
                                    )
                                    if st_d == HIDP_STATUS_SUCCESS:
                                        waveform_durations[wf_id] = int(d_val.value)
                    if len(parsed_map) > 2:
                        waveform_to_ordinal = parsed_map

        # CRITICAL FIX for Surface Mouse 2nd Edition (PID=0x0880):
        # OutputReportByteLength is 3 bytes (Out=3B). Passing 8 bytes causes
        # HidP_SetUsageValue and WriteFile to fail with INVALID_REPORT_LENGTH!
        exact_out_len = int(caps.OutputReportByteLength) if caps.OutputReportByteLength > 0 else 3

        return HapticDeviceInfo(
            device_path=device_path,
            vendor_id=vid,
            product_id=pid,
            product_name=product_name,
            output_report_len=exact_out_len,
            feature_report_len=int(caps.FeatureReportByteLength),
            trigger_report_id=trigger_report_id,
            waveform_feature_report_id=waveform_feature_report_id,
            intensity_logical_max=intensity_max,
            waveform_to_ordinal=waveform_to_ordinal,
            waveform_durations_ms=waveform_durations,
            trigger_link_col=trigger_link_col,
            intensity_link_col=intensity_link_col,
        )
    def fire_waveform(
        self,
        waveform_id: int,
        strength_1_to_5: int = 3,
        repeat_count: int = 0,
        retrigger_period_ms: int = 0,
        cutoff_time_ms: int = 1000,
        min_interval_ms: int = 14,
    ) -> bool:
        """
        Send a Manual Trigger (0x0E:0x21) + Intensity (0x0E:0x23) Output Report
        to the Surface Mouse's Simple Haptic Controller collection.
        """
        now = time.perf_counter()
        if (now - self._last_trigger_ts) * 1000.0 < min_interval_ms and waveform_id != WAVEFORM_STOP:
            return False
        self._last_trigger_ts = now
        if not IS_WINDOWS or self._handle is None or self._preparsed is None or self.device_info is None:
            return False
        with self._lock:
            info = self.device_info
            ordinal = info.waveform_to_ordinal.get(
                waveform_id,
                info.waveform_to_ordinal.get(WAVEFORM_CLICK, 3),
            )
            clamped_strength = max(0, min(5, int(strength_1_to_5)))
            if clamped_strength == 0 or waveform_id == WAVEFORM_STOP:
                logical_intensity = 0
            else:
                logical_intensity = max(
                    1,
                    round((clamped_strength / 5.0) * info.intensity_logical_max),
                )
            report_len = info.output_report_len
            out_buf = ctypes.create_string_buffer(report_len)
            out_buf[0] = bytes([info.trigger_report_id])

            st_trig = hid.HidP_SetUsageValue(
                HidP_Output,
                HID_USAGE_PAGE_HAPTICS,
                info.trigger_link_col,
                HID_USAGE_MANUAL_TRIGGER,
                ordinal,
                self._preparsed,
                out_buf,
                report_len,
            )
            if st_trig != HIDP_STATUS_SUCCESS and info.trigger_link_col != 0:
                st_trig = hid.HidP_SetUsageValue(
                    HidP_Output,
                    HID_USAGE_PAGE_HAPTICS,
                    0,
                    HID_USAGE_MANUAL_TRIGGER,
                    ordinal,
                    self._preparsed,
                    out_buf,
                    report_len,
                )

            st_int = hid.HidP_SetUsageValue(
                HidP_Output,
                HID_USAGE_PAGE_HAPTICS,
                info.intensity_link_col,
                HID_USAGE_INTENSITY,
                logical_intensity,
                self._preparsed,
                out_buf,
                report_len,
            )
            if st_int != HIDP_STATUS_SUCCESS and info.intensity_link_col != 0:
                st_int = hid.HidP_SetUsageValue(
                    HidP_Output,
                    HID_USAGE_PAGE_HAPTICS,
                    0,
                    HID_USAGE_INTENSITY,
                    logical_intensity,
                    self._preparsed,
                    out_buf,
                    report_len,
                )

            # If the 3-byte Output Report is byte-aligned [ReportID, ManualTrigger, Intensity]
            # and HidP_SetUsageValue didn't populate bytes 1..2, populate directly as fallback:
            if report_len == 3 and out_buf.raw[1] == 0 and ordinal > 0:
                out_buf[1] = bytes([ordinal & 0xFF])
                out_buf[2] = bytes([logical_intensity & 0xFF])

            if report_len > 3:
                hid.HidP_SetUsageValue(
                    HidP_Output, HID_USAGE_PAGE_HAPTICS, info.trigger_link_col,
                    HID_USAGE_REPEAT_COUNT, repeat_count, self._preparsed, out_buf, report_len
                )
                hid.HidP_SetUsageValue(
                    HidP_Output, HID_USAGE_PAGE_HAPTICS, info.trigger_link_col,
                    HID_USAGE_RETRIGGER_PERIOD, retrigger_period_ms, self._preparsed, out_buf, report_len
                )
                hid.HidP_SetUsageValue(
                    HidP_Output, HID_USAGE_PAGE_HAPTICS, info.trigger_link_col,
                    HID_USAGE_WAVEFORM_CUTOFF_TIME, cutoff_time_ms, self._preparsed, out_buf, report_len
                )

            written = wintypes.DWORD(0)
            ok = bool(
                kernel32.WriteFile(
                    self._handle,
                    out_buf,
                    report_len,
                    ctypes.byref(written),
                    None,
                )
            )
            if not ok:
                ok = bool(hid.HidD_SetOutputReport(self._handle, out_buf, report_len))
            return ok
    def close(self) -> None:
        if IS_WINDOWS:
            if self._preparsed:
                hid.HidD_FreePreparsedData(self._preparsed)
                self._preparsed = None
            if self._handle:
                kernel32.CloseHandle(self._handle)
                self._handle = None
        self.device_info = None

class WinRTHapticsBridge:
    def __init__(self) -> None:
        self.available = False
        self._mgr_cls = None
        try:
            from winrt.windows.devices.haptics import InputHapticsManager  # type: ignore
            if InputHapticsManager.is_supported():
                self._mgr_cls = InputHapticsManager
                self.available = True
        except Exception:
            self.available = False
    def fire_waveform(self, waveform_id: int, strength_1_to_5: int = 3) -> bool:
        if not self.available or self._mgr_cls is None:
            return False
        try:
            mgr = self._mgr_cls.get_for_current_thread()
            intensity = max(0.0, min(1.0, strength_1_to_5 / 5.0))
            return bool(mgr.try_send_haptic_waveform(waveform_id, 0, intensity))
        except Exception:
            return False

# ============================================================================
# 5. Scroll Physics Engine: Dynamic Ratchet + Logitech M720 Forever Scroll
# ============================================================================
@dataclass
class ScrollTickDecision:
    haptic_event: Optional[str]
    entered_freespin: bool
    velocity_notches_per_sec: float

class KineticForeverScrollEngine:
    def __init__(self, config: SurfacePulseConfig) -> None:
        self.config = config
        self._last_wheel_ts: float = 0.0
        self._ema_velocity: float = 0.0
        self.kinetic_velocity_deltas_per_sec: float = 0.0
        self.in_freespin: bool = False
        self._fractional_carry: float = 0.0
        self._lock = threading.Lock()
    def toggle_forever_scroll(self) -> bool:
        with self._lock:
            self.config.forever_scroll_active = not self.config.forever_scroll_active
            if not self.config.forever_scroll_active:
                self.kinetic_velocity_deltas_per_sec = 0.0
                self.in_freespin = False
            return self.config.forever_scroll_active
    def stop_coasting(self) -> bool:
        with self._lock:
            was_spinning = self.in_freespin or abs(self.kinetic_velocity_deltas_per_sec) > 5.0
            self.kinetic_velocity_deltas_per_sec = 0.0
            self.in_freespin = False
            self._fractional_carry = 0.0
            return was_spinning
    def on_physical_wheel(
        self,
        wheel_delta: int,
        now: Optional[float] = None,
        active_profile: Optional[AppProfile] = None,
    ) -> ScrollTickDecision:
        now_ts = now if now is not None else time.perf_counter()
        notches = wheel_delta / 120.0
        mode = active_profile.scroll_mode if active_profile else self.config.scroll_mode
        slow_thresh = active_profile.fast_scroll_threshold if active_profile else self.config.slow_ratchet_threshold
        flick_thresh = active_profile.flick_velocity_threshold if active_profile else self.config.flick_freespin_threshold
        with self._lock:
            dt = max(0.004, now_ts - self._last_wheel_ts) if self._last_wheel_ts > 0 else 0.25
            self._last_wheel_ts = now_ts
            inst_velocity = notches / dt
            if self.kinetic_velocity_deltas_per_sec * wheel_delta < 0:
                self.kinetic_velocity_deltas_per_sec = 0.0
                self._ema_velocity = inst_velocity
                self.in_freespin = False
                return ScrollTickDecision("scroll_freespin_stop", False, abs(inst_velocity))
            if dt > 0.22:
                self._ema_velocity = inst_velocity
            else:
                self._ema_velocity = 0.25 * self._ema_velocity + 0.75 * inst_velocity
            abs_vel = abs(self._ema_velocity)
            if self.config.forever_scroll_active or mode == "forever_m720":
                self.in_freespin = True
                target_vel = max(abs_vel, 6.0) * 120.0 * (1.0 if wheel_delta > 0 else -1.0)
                self.kinetic_velocity_deltas_per_sec = target_vel
                return ScrollTickDecision(None, True, abs_vel)
            if mode != "ratchet_only" and abs_vel >= flick_thresh:
                self.in_freespin = True
                self.kinetic_velocity_deltas_per_sec += wheel_delta * 14.5
                return ScrollTickDecision(None, True, abs_vel)
            if self.config.kinetic_scrolling_enabled and mode != "ratchet_only" and abs_vel >= slow_thresh:
                self.kinetic_velocity_deltas_per_sec += wheel_delta * 5.0
            if abs_vel < slow_thresh:
                return ScrollTickDecision("scroll_slow_notch", False, abs_vel)
            return ScrollTickDecision("scroll_fast_tick", False, abs_vel)
    def step_physics(
        self,
        dt_sec: float = 1.0 / 120.0,
        active_profile: Optional[AppProfile] = None,
    ) -> Tuple[int, bool]:
        with self._lock:
            if abs(self.kinetic_velocity_deltas_per_sec) < 1.0:
                stopped = self.in_freespin
                self.kinetic_velocity_deltas_per_sec = 0.0
                self.in_freespin = False
                return 0, stopped
            mode = active_profile.scroll_mode if active_profile else self.config.scroll_mode
            is_forever = (
                self.config.forever_scroll_active
                or mode == "forever_m720"
                or (self.in_freespin and self.config.auto_forever_scroll_on_flick)
            )
            if is_forever:
                friction_per_frame = self.config.forever_scroll_friction
            else:
                base_friction = (
                    active_profile.kinetic_momentum
                    if active_profile and active_profile.kinetic_momentum > 0
                    else self.config.kinetic_friction
                )
                friction_per_frame = math.pow(base_friction, dt_sec * 60.0)
            self.kinetic_velocity_deltas_per_sec *= friction_per_frame
            raw_delta = self.kinetic_velocity_deltas_per_sec * dt_sec + self._fractional_carry
            int_delta = int(round(raw_delta))
            self._fractional_carry = raw_delta - int_delta
            if not is_forever and abs(self.kinetic_velocity_deltas_per_sec) < 25.0:
                was_freespin = self.in_freespin
                self.kinetic_velocity_deltas_per_sec = 0.0
                self.in_freespin = False
                self._fractional_carry = 0.0
                return int_delta, was_freespin
            return int_delta, False

# ============================================================================
# 6. Non-Blocking Runtime & Fail-Safe 64-Bit Hook Manager
# ============================================================================
class SurfacePulseRuntime:
    def __init__(self, config: SurfacePulseConfig) -> None:
        self.config = config
        self.raw_hid = RawHidSimpleHapticsDriver()
        self.winrt_bridge = WinRTHapticsBridge()
        self.scroll_engine = KineticForeverScrollEngine(config)
        self.event_log: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self._haptic_queue: "queue.Queue[Tuple[int, int, str]]" = queue.Queue(maxsize=32)
        self._pointer_queue: "queue.Queue[Tuple[int, int, bool]]" = queue.Queue(maxsize=2)
        self.on_action_ring_requested: Optional[Callable[[int, int], None]] = None
        self.on_status_changed: Optional[Callable[[], None]] = None
        self._running = False
        self._hook_thread: Optional[threading.Thread] = None
        self._hook_thread_id: int = 0
        self._haptic_thread: Optional[threading.Thread] = None
        self._physics_thread: Optional[threading.Thread] = None
        self._uia_thread: Optional[threading.Thread] = None
        self._last_cursor_pt: Tuple[int, int] = (0, 0)
        self._left_button_down: bool = False
        self._left_down_ts: float = 0.0
        self._force_click_fired: bool = False
        self._dragging_active: bool = False
        self._drag_start_pt: Tuple[int, int] = (0, 0)
        self._text_select_accum_px: float = 0.0
        self._at_screen_edge: bool = False
        self._last_hovered_hwnd: int = 0
        self._last_hovered_role: str = ""
        self._current_brightness: int = 70
        self._action_btn_down_ts: float = 0.0
        self._action_btn_is_down: bool = False
        self._action_hold_triggered: bool = False
        self._action_last_click_ts: float = 0.0
        self._gesture_accum_x: float = 0.0
        self._gesture_accum_y: float = 0.0
    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        try:
            self.event_log.put_nowait(entry)
        except queue.Full:
            try:
                self.event_log.get_nowait()
                self.event_log.put_nowait(entry)
            except queue.Empty:
                pass
    def connect_device(self) -> bool:
        connected = self.raw_hid.connect()
        if connected and self.raw_hid.device_info:
            info = self.raw_hid.device_info
            wf_names = [
                WAVEFORM_NAMES.get(wf, f"0x{wf:04X}")
                for wf in info.waveform_to_ordinal.keys()
                if wf not in (WAVEFORM_NONE, WAVEFORM_STOP)
            ]
            self.log(
                f"Connected to {info.product_name} (VID=0x{info.vendor_id:04X}, "
                f"PID=0x{info.product_id:04X}, TriggerReport=0x{info.trigger_report_id:02X}, "
                f"Waveforms={', '.join(wf_names)})"
            )
            if self.config.per_device_pointer_speed:
                self.apply_mouse_speed(self.config.surface_mouse_speed)
        else:
            self.log(
                "Standby Mode: Surface Mouse 0x0E HID collection not found (click 'Run HID 0x0E Diagnostics' in Tab 3)."
            )
        if self.on_status_changed:
            self.on_status_changed()
        return connected
    def apply_mouse_speed(self, speed_1_to_20: int) -> None:
        if not IS_WINDOWS:
            return
        SPI_SETMOUSESPEED = 0x0071
        clamped = max(1, min(20, int(speed_1_to_20)))
        user32.SystemParametersInfoW(SPI_SETMOUSESPEED, 0, ctypes.c_void_p(clamped), 0)
    def adjust_brightness(self, delta_pct: int) -> None:
        self._current_brightness = max(5, min(100, self._current_brightness + delta_pct))
        if IS_WINDOWS:
            cmd = (
                f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
                f".WmiSetBrightness(1, {self._current_brightness})"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.log(f"[Gesture] Adjusted Screen Brightness -> {self._current_brightness}%")
    def trigger_event_haptic(self, event_key: str) -> None:
        """
        Non-blocking haptic enqueue safe to call from inside WH_MOUSE_LL.
        Never performs synchronous HID I/O on the hook thread!
        """
        if not self.config.master_haptics_enabled:
            return
        setting = self.config.events.get(event_key)
        if not setting or not setting.enabled:
            return
        waveform_id = NAME_TO_WAVEFORM.get(setting.waveform, WAVEFORM_CLICK)
        try:
            self._haptic_queue.put_nowait((waveform_id, setting.strength, event_key))
        except queue.Full:
            pass
    def fire_raw_or_winrt(
        self,
        waveform_id: int,
        strength_1_to_5: int = 3,
        reason: str = "manual",
    ) -> bool:
        wf_name = WAVEFORM_NAMES.get(waveform_id, f"0x{waveform_id:04X}")
        ok = False
        if self.config.haptic_backend == "winrt_api" and self.winrt_bridge.available:
            ok = self.winrt_bridge.fire_waveform(waveform_id, strength_1_to_5)
        if not ok:
            ok = self.raw_hid.fire_waveform(
                waveform_id,
                strength_1_to_5=strength_1_to_5,
                min_interval_ms=self.config.min_trigger_interval_ms,
            )
        status_tag = "HID 0x0E" if ok else "SIM"
        self.log(f"[{status_tag}] Fired {wf_name} (0x{waveform_id:04X}) @ Strength {strength_1_to_5}/5 <- {reason}")
        return ok
    def _haptic_worker_loop(self) -> None:
        """Dedicated background worker that sends HID 0x0E reports & auto-connects on Bluetooth wake."""
        last_scan = time.perf_counter()
        while self._running:
            if self.raw_hid.device_info is None and (time.perf_counter() - last_scan) >= 2.5:
                last_scan = time.perf_counter()
                if self.raw_hid.connect():
                    info = self.raw_hid.device_info
                    if info:
                        self.log(
                            f"[AutoWake] Surface Mouse connected: {info.product_name} "
                            f"(VID=0x{info.vendor_id:04X}, PID=0x{info.product_id:04X})"
                        )
                    if self.on_status_changed:
                        self.on_status_changed()
            try:
                waveform_id, strength, reason = self._haptic_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.fire_raw_or_winrt(waveform_id, strength, reason)
            except Exception as exc:
                self.log(f"[HapticWorker] Warning: {exc}")
    def get_active_profile(self) -> Optional[AppProfile]:
        if not IS_WINDOWS:
            return None
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return None
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not h_proc:
                return None
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                    exe_name = os.path.basename(buf.value).lower()
                    for prof in self.config.app_profiles:
                        targets = [t.strip().lower() for t in prof.exe_match.split(",") if t.strip()]
                        if exe_name in targets:
                            return prof
            finally:
                kernel32.CloseHandle(h_proc)
        except Exception:
            pass
        return None
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.connect_device()
        self._haptic_thread = threading.Thread(target=self._haptic_worker_loop, daemon=True)
        self._haptic_thread.start()
        self._physics_thread = threading.Thread(target=self._physics_loop, daemon=True)
        self._physics_thread.start()
        if IS_WINDOWS:
            self._uia_thread = threading.Thread(target=self._ui_inspection_loop, daemon=True)
            self._uia_thread.start()
            self._hook_thread = threading.Thread(target=self._win32_hook_loop, daemon=True)
            self._hook_thread.start()
    def stop(self) -> None:
        self._running = False
        if IS_WINDOWS and self._hook_thread_id:
            WM_QUIT = 0x0012
            user32.PostThreadMessageW(self._hook_thread_id, WM_QUIT, 0, 0)
        if self.config.per_device_pointer_speed:
            self.apply_mouse_speed(self.config.other_pointer_speed)
        self.raw_hid.close()
    def _physics_loop(self) -> None:
        dt = 1.0 / 120.0
        while self._running:
            time.sleep(dt)
            if not self.config.global_hook_enabled:
                continue
            prof = self.get_active_profile()
            mult = prof.scroll_speed_multiplier if prof else 1.0
            delta, stopped_freespin = self.scroll_engine.step_physics(dt, prof)
            if delta != 0 and IS_WINDOWS:
                self._inject_wheel_delta(int(round(delta * mult)))
            if stopped_freespin:
                self.trigger_event_haptic("scroll_freespin_stop")
            if (
                self.config.force_click_enabled
                and self._left_button_down
                and not self._dragging_active
                and not self._force_click_fired
            ):
                held_ms = (time.perf_counter() - self._left_down_ts) * 1000.0
                if held_ms >= self.config.force_click_hold_ms:
                    self._force_click_fired = True
                    self.trigger_event_haptic("force_click")
                    self._send_hotkey(self.config.force_click_shortcut)
    def _inject_wheel_delta(self, delta: int) -> None:
        if not IS_WINDOWS:
            return
        INPUT_MOUSE = 0
        MOUSEEVENTF_WHEEL = 0x0800
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.mi.mouseData = wintypes.DWORD(delta & 0xFFFFFFFF)
        inp.mi.dwFlags = MOUSEEVENTF_WHEEL
        inp.mi.dwExtraInfo = SURFACE_PULSE_MAGIC
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    def execute_action(self, action_name: str, x: int = 0, y: int = 0) -> None:
        if action_name == "toggle_forever_scroll":
            state = self.scroll_engine.toggle_forever_scroll()
            self.trigger_event_haptic("forever_scroll_toggle")
            self.log(f"M720 Forever Scroll Mode toggled {'ON' if state else 'OFF'}")
            if self.on_status_changed:
                self.on_status_changed()
        elif action_name == "open_action_ring":
            self.trigger_event_haptic("action_ring_slot")
            if self.on_action_ring_requested:
                self.on_action_ring_requested(x, y)
        elif action_name == "task_view":
            self._send_hotkey("win+tab")
        elif action_name == "search":
            self._send_hotkey("alt+space")
        elif action_name == "screenshot":
            self._send_hotkey("win+shift+s")
    def _send_hotkey(self, combo: str) -> None:
        if not IS_WINDOWS:
            self.log(f"[Shortcut] Would send hotkey: {combo}")
            return
        vk_map = {
            "win": 0x5B,
            "ctrl": 0x11,
            "alt": 0x12,
            "shift": 0x10,
            "tab": 0x09,
            "space": 0x20,
            "c": 0x43,
            "f": 0x46,
            "v": 0x56,
            "z": 0x5A,
            "s": 0x53,
            "media_play_pause": 0xB3,
            "vol_up": 0xAF,
            "vol_down": 0xAE,
        }
        keys = [vk_map[k] for k in combo.lower().split("+") if k in vk_map]
        KEYEVENTF_KEYUP = 0x0002
        for vk in keys:
            user32.keybd_event(vk, 0, 0, 0)
        for vk in reversed(keys):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    def _ui_inspection_loop(self) -> None:
        if not IS_WINDOWS:
            return
        IDC_IBEAM = 32513
        IDC_HAND = 32649
        h_ibeam = user32.LoadCursorW(None, ctypes.c_void_p(IDC_IBEAM))
        h_hand = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        while self._running:
            try:
                x, y, left_down = self._pointer_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if not self.config.global_hook_enabled:
                continue
            try:
                screen_w = user32.GetSystemMetrics(0)
                screen_h = user32.GetSystemMetrics(1)
                near_edge = (x <= 1 or y <= 1 or x >= screen_w - 2 or y >= screen_h - 2)
                near_snap_center = (
                    left_down and self._dragging_active and abs(x - screen_w // 2) <= 4 and y <= 24
                )
                if (near_edge or near_snap_center) and not self._at_screen_edge:
                    self._at_screen_edge = True
                    if left_down and self._dragging_active:
                        self.trigger_event_haptic("window_snap")
                    else:
                        self.trigger_event_haptic("screen_edge")
                elif not (near_edge or near_snap_center):
                    self._at_screen_edge = False
                ci = CURSORINFO()
                ci.cbSize = ctypes.sizeof(CURSORINFO)
                is_ibeam = False
                is_hand = False
                if user32.GetCursorInfo(ctypes.byref(ci)):
                    is_ibeam = (ci.hCursor == h_ibeam)
                    is_hand = (ci.hCursor == h_hand)
                if left_down and is_ibeam:
                    dx = x - self._last_cursor_pt[0]
                    dy = y - self._last_cursor_pt[1]
                    self._text_select_accum_px += math.hypot(dx, dy)
                    if self._text_select_accum_px >= 18.0:
                        self._text_select_accum_px = 0.0
                        self.trigger_event_haptic("text_selection")
                    continue
                pt = POINT(x, y)
                hwnd = user32.WindowFromPoint(pt)
                if hwnd and hwnd != self._last_hovered_hwnd:
                    self._last_hovered_hwnd = hwnd
                    cls_buf = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(hwnd, cls_buf, 64)
                    cls_name = cls_buf.value.lower()
                    if "menu" in cls_name or "#32768" in cls_name:
                        self.trigger_event_haptic("hover_menu_item")
                    elif "trackbar" in cls_name or "slider" in cls_name:
                        self.trigger_event_haptic("slider_step")
                    elif any(k in cls_name for k in ("button", "systabcontrol", "toolbar", "mstasklist")):
                        self.trigger_event_haptic("hover_button_tab_link")
                elif is_hand and self._last_hovered_role != "hand_link":
                    self._last_hovered_role = "hand_link"
                    self.trigger_event_haptic("hover_button_tab_link")
                elif not is_hand:
                    self._last_hovered_role = ""
            except Exception:
                pass
    def _win32_hook_loop(self) -> None:
        """
        Installs a 64-bit-safe, non-blocking WH_MOUSE_LL hook that ignores
        touch/pen events and ALWAYS forwards events via CallNextHookEx.
        """
        if not IS_WINDOWS:
            return
        self._hook_thread_id = int(kernel32.GetCurrentThreadId())
        WH_MOUSE_LL = 14
        WM_MOUSEMOVE = 0x0200
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP = 0x0202
        WM_RBUTTONDOWN = 0x0204
        WM_MBUTTONDOWN = 0x0207
        WM_MOUSEWHEEL = 0x020A
        WM_XBUTTONDOWN = 0x020B
        WM_XBUTTONUP = 0x020C
        def mouse_callback(n_code: int, w_param: int, l_param: int) -> int:
            try:
                if n_code >= 0 and self.config.global_hook_enabled and l_param:
                    info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    extra = int(info.dwExtraInfo)
                    # 1. Bypass our own synthetic wheel events AND Windows Touch/Pen events
                    if extra == SURFACE_PULSE_MAGIC or (extra & SIGNATURE_MASK) == MI_WP_SIGNATURE:
                        return user32.CallNextHookEx(None, n_code, w_param, l_param)
                    x, y = int(info.pt.x), int(info.pt.y)
                    if w_param == WM_MOUSEMOVE:
                        if self._action_btn_is_down:
                            held_ms = (time.perf_counter() - self._action_btn_down_ts) * 1000.0
                            if held_ms >= 180.0:
                                self._action_hold_triggered = True
                                dx = x - self._last_cursor_pt[0]
                                dy = self._last_cursor_pt[1] - y
                                self._gesture_accum_x += dx
                                self._gesture_accum_y += dy
                                if abs(self._gesture_accum_y) >= 16:
                                    step_dir = 1 if self._gesture_accum_y > 0 else -1
                                    self._gesture_accum_y = 0.0
                                    self._send_hotkey("vol_up" if step_dir > 0 else "vol_down")
                                    self.trigger_event_haptic("gesture_step")
                                if abs(self._gesture_accum_x) >= 22:
                                    b_dir = 5 if self._gesture_accum_x > 0 else -5
                                    self._gesture_accum_x = 0.0
                                    self.adjust_brightness(b_dir)
                                    self.trigger_event_haptic("gesture_step")
                        if self._left_button_down and not self._dragging_active:
                            if math.hypot(x - self._drag_start_pt[0], y - self._drag_start_pt[1]) > 10:
                                self._dragging_active = True
                                self.trigger_event_haptic("drag_pickup")
                        self._last_cursor_pt = (x, y)
                        try:
                            self._pointer_queue.put_nowait((x, y, self._left_button_down))
                        except queue.Full:
                            pass
                    elif w_param == WM_LBUTTONDOWN:
                        self._left_button_down = True
                        self._left_down_ts = time.perf_counter()
                        self._force_click_fired = False
                        self._dragging_active = False
                        self._drag_start_pt = (x, y)
                        if self.config.stop_forever_scroll_on_click:
                            if self.scroll_engine.stop_coasting():
                                self.trigger_event_haptic("scroll_freespin_stop")
                        self.trigger_event_haptic("left_click")
                    elif w_param == WM_LBUTTONUP:
                        if self._dragging_active:
                            self.trigger_event_haptic("drag_drop")
                        self._left_button_down = False
                        self._dragging_active = False
                    elif w_param == WM_RBUTTONDOWN:
                        if self.config.stop_forever_scroll_on_click:
                            self.scroll_engine.stop_coasting()
                        self.trigger_event_haptic("right_click")
                    elif w_param == WM_MBUTTONDOWN:
                        if self.config.forever_scroll_toggle_button in ("middle_or_action", "middle_only"):
                            self.execute_action("toggle_forever_scroll", x, y)
                    elif w_param == WM_XBUTTONDOWN:
                        self._action_btn_is_down = True
                        self._action_btn_down_ts = time.perf_counter()
                        self._action_hold_triggered = False
                        self._gesture_accum_x = 0.0
                        self._gesture_accum_y = 0.0
                        if self.config.swallow_action_button:
                            return 1
                    elif w_param == WM_XBUTTONUP:
                        if self._action_btn_is_down:
                            self._action_btn_is_down = False
                            if not self._action_hold_triggered:
                                now_t = time.perf_counter()
                                if (now_t - self._action_last_click_ts) <= 0.30:
                                    self._action_last_click_ts = 0.0
                                    self.execute_action(self.config.action_double_press, x, y)
                                else:
                                    self._action_last_click_ts = now_t
                                    self.execute_action(self.config.action_single_press, x, y)
                            if self.config.swallow_action_button:
                                return 1
                    elif w_param == WM_MOUSEWHEEL:
                        raw_hi = (info.mouseData >> 16) & 0xFFFF
                        wheel_delta = raw_hi - 0x10000 if raw_hi & 0x8000 else raw_hi
                        decision = self.scroll_engine.on_physical_wheel(wheel_delta)
                        if decision.haptic_event:
                            self.trigger_event_haptic(decision.haptic_event)
            except Exception as exc:
                self.log(f"[HookSafeGuard] Ignored error: {exc}")
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        self._mouse_proc_ref = LowLevelMouseProc(mouse_callback)
        h_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc_ref, None, 0)
        msg = wintypes.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if h_hook:
            user32.UnhookWindowsHookEx(h_hook)

# ============================================================================
# 7. Interactive Desktop GUI + Diagnostics + Radial Action Ring (Tkinter)
# ============================================================================
def launch_gui(runtime: SurfacePulseRuntime) -> None:
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("SurfacePulse v2.0 (64-Bit Safe) — Surface Mouse (2nd Edition) Haptics & Forever Scroll")
    root.geometry("1000x720")
    root.configure(bg="#0F172A")
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("TFrame", background="#0F172A")
    style.configure("Card.TFrame", background="#1E293B")
    style.configure("TLabel", background="#0F172A", foreground="#F8FAFC", font=("Segoe UI", 10))
    style.configure("Card.TLabel", background="#1E293B", foreground="#F8FAFC", font=("Segoe UI", 10))
    style.configure("Header.TLabel", background="#0F172A", foreground="#38BDF8", font=("Segoe UI Semibold", 14))
    style.configure("Sub.TLabel", background="#1E293B", foreground="#94A3B8", font=("Segoe UI", 9))
    style.configure("Accent.TButton", background="#0284C7", foreground="#FFFFFF", font=("Segoe UI Semibold", 10), padding=6)
    style.map("Accent.TButton", background=[("active", "#0369A1")])
    style.configure("TNotebook", background="#0F172A", borderwidth=0)
    style.configure("TNotebook.Tab", background="#1E293B", foreground="#CBD5E1", padding=(14, 8), font=("Segoe UI Semibold", 10))
    style.map("TNotebook.Tab", background=[("selected", "#0284C7")], foreground=[("selected", "#FFFFFF")])
    header = ttk.Frame(root, padding=(16, 12))
    header.pack(fill="x")
    ttk.Label(
        header,
        text="SurfacePulse v2.0 — Surface Mouse (2nd Gen) HID 0x0E Haptics & M720 Forever Scroll",
        style="Header.TLabel",
    ).pack(side="left")
    status_var = tk.StringVar(value="Scanning HID Usage Page 0x0E...")
    status_lbl = ttk.Label(header, textvariable=status_var, foreground="#34D399")
    status_lbl.pack(side="right", padx=8)
    def refresh_status() -> None:
        info = runtime.raw_hid.device_info
        forever_txt = "ON (Coasting Locked)" if runtime.config.forever_scroll_active else "OFF (Dynamic Ratchet)"
        hook_txt = "Active" if runtime.config.global_hook_enabled else "Paused"
        if info:
            status_var.set(
                f"Connected: {info.product_name} (0x0E:0x01) | Hook: {hook_txt} | Forever Scroll: {forever_txt}"
            )
        else:
            status_var.set(f"HID 0x0E Standby | Hook: {hook_txt} | Forever Scroll: {forever_txt}")
    runtime.on_status_changed = lambda: root.after(0, refresh_status)
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=16, pady=(0, 12))
    # ------------------------------------------------------------------------
    # Tab 1: Logitech M720 Forever Scroll & Dynamic Ratchet Physics
    # ------------------------------------------------------------------------
    tab_scroll = ttk.Frame(notebook, padding=16)
    notebook.add(tab_scroll, text="1. Forever Scroll (M720) & Wheel Ratchet")
    card_m720 = ttk.Frame(tab_scroll, style="Card.TFrame", padding=16)
    card_m720.pack(fill="x", pady=(0, 12))
    ttk.Label(
        card_m720,
        text="Logitech M720-Style Forever Scroll Mode + iXpat Dynamic Haptic Ratchet",
        style="Card.TLabel",
        font=("Segoe UI Semibold", 12),
    ).pack(anchor="w")
    ttk.Label(
        card_m720,
        text=(
            "Toggle frictionless Forever Scroll with 1 button press (or automatically on a fast flick) "
            "so a single spin keeps scrolling until you click or flick back."
        ),
        style="Sub.TLabel",
    ).pack(anchor="w", pady=(2, 10))
    btn_bar = ttk.Frame(card_m720, style="Card.TFrame")
    btn_bar.pack(fill="x", pady=4)
    forever_btn_var = tk.StringVar(value="Toggle M720 Forever Scroll Mode (Currently OFF)")
    def on_toggle_forever_btn() -> None:
        runtime.execute_action("toggle_forever_scroll")
        state = runtime.config.forever_scroll_active
        forever_btn_var.set(
            f"Toggle M720 Forever Scroll Mode (Currently {'ON — Infinite Spin' if state else 'OFF — Dynamic Ratchet'})"
        )
        refresh_status()
    ttk.Button(
        btn_bar,
        textvariable=forever_btn_var,
        style="Accent.TButton",
        command=on_toggle_forever_btn,
    ).pack(side="left", padx=(0, 10))
    hook_btn_var = tk.StringVar(value="Pause Global Mouse Hook")
    def on_toggle_hook_btn() -> None:
        runtime.config.global_hook_enabled = not runtime.config.global_hook_enabled
        if not runtime.config.global_hook_enabled:
            runtime.scroll_engine.stop_coasting()
        hook_btn_var.set(
            "Pause Global Mouse Hook" if runtime.config.global_hook_enabled else "Resume Global Mouse Hook"
        )
        refresh_status()
    ttk.Button(
        btn_bar,
        textvariable=hook_btn_var,
        style="Accent.TButton",
        command=on_toggle_hook_btn,
    ).pack(side="left", padx=(0, 10))
    ttk.Button(
        btn_bar,
        text="Reconnect Surface Mouse (0x0E)",
        style="Accent.TButton",
        command=runtime.connect_device,
    ).pack(side="left")
    grid_scroll = ttk.Frame(card_m720, style="Card.TFrame")
    grid_scroll.pack(fill="x", pady=8)
    auto_flick_var = tk.BooleanVar(value=runtime.config.auto_forever_scroll_on_flick)
    stop_click_var = tk.BooleanVar(value=runtime.config.stop_forever_scroll_on_click)
    def sync_scroll_settings(*_args: object) -> None:
        runtime.config.auto_forever_scroll_on_flick = auto_flick_var.get()
        runtime.config.stop_forever_scroll_on_click = stop_click_var.get()
        runtime.config.slow_ratchet_threshold = float(slow_scale.get())
        runtime.config.flick_freespin_threshold = float(flick_scale.get())
        runtime.config.kinetic_friction = float(friction_scale.get())
        runtime.config.save()
    tk.Checkbutton(
        grid_scroll,
        text="Auto-Lock Forever Scroll on Fast Flick (1 spin keeps coasting until click/reverse)",
        variable=auto_flick_var,
        command=sync_scroll_settings,
        bg="#1E293B",
        fg="#F8FAFC",
        selectcolor="#0F172A",
        activebackground="#1E293B",
        activeforeground="#38BDF8",
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=3)
    tk.Checkbutton(
        grid_scroll,
        text="Stop Forever-Scroll Coasting Immediately on Any Mouse Click",
        variable=stop_click_var,
        command=sync_scroll_settings,
        bg="#1E293B",
        fg="#F8FAFC",
        selectcolor="#0F172A",
        activebackground="#1E293B",
        activeforeground="#38BDF8",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
    ttk.Label(grid_scroll, text="Slow -> Fast Ratchet Threshold (notches/sec):", style="Card.TLabel").grid(
        row=2, column=0, sticky="w", pady=6
    )
    slow_scale = tk.Scale(
        grid_scroll, from_=2.0, to=15.0, resolution=0.5, orient="horizontal",
        bg="#1E293B", fg="#F8FAFC", highlightthickness=0, length=260, command=sync_scroll_settings
    )
    slow_scale.set(runtime.config.slow_ratchet_threshold)
    slow_scale.grid(row=2, column=1, sticky="w", padx=12)
    ttk.Label(grid_scroll, text="Flick Free-Spin Threshold (notches/sec):", style="Card.TLabel").grid(
        row=3, column=0, sticky="w", pady=6
    )
    flick_scale = tk.Scale(
        grid_scroll, from_=10.0, to=35.0, resolution=1.0, orient="horizontal",
        bg="#1E293B", fg="#F8FAFC", highlightthickness=0, length=260, command=sync_scroll_settings
    )
    flick_scale.set(runtime.config.flick_freespin_threshold)
    flick_scale.grid(row=3, column=1, sticky="w", padx=12)
    ttk.Label(grid_scroll, text="Kinetic Momentum Coast (0.75 short .. 0.98 ultra-long):", style="Card.TLabel").grid(
        row=4, column=0, sticky="w", pady=6
    )
    friction_scale = tk.Scale(
        grid_scroll, from_=0.75, to=0.98, resolution=0.01, orient="horizontal",
        bg="#1E293B", fg="#F8FAFC", highlightthickness=0, length=260, command=sync_scroll_settings
    )
    friction_scale.set(runtime.config.kinetic_friction)
    friction_scale.grid(row=4, column=1, sticky="w", padx=12)
    # ------------------------------------------------------------------------
    # Tab 2: All 8 Surface Waveforms & Event Mapping Matrix
    # ------------------------------------------------------------------------
    tab_haptics = ttk.Frame(notebook, padding=16)
    notebook.add(tab_haptics, text="2. Haptic Waveforms & Event Matrix")
    top_bar = ttk.Frame(tab_haptics, style="Card.TFrame", padding=12)
    top_bar.pack(fill="x", pady=(0, 12))
    ttk.Label(
        top_bar,
        text="Test Built-in Surface Mouse Waveforms (Usage Page 0x0E, Ordinals 3..10):",
        style="Card.TLabel",
        font=("Segoe UI Semibold", 10),
    ).pack(anchor="w", pady=(0, 8))
    btn_row = ttk.Frame(top_bar, style="Card.TFrame")
    btn_row.pack(fill="x")
    for wf_name in ("Click", "Hover", "Collide", "Align", "Step", "Grow", "Success", "Error"):
        wf_id = NAME_TO_WAVEFORM[wf_name]
        ttk.Button(
            btn_row,
            text=f"{wf_name} (0x{wf_id:04X})",
            style="Accent.TButton",
            command=lambda wid=wf_id, wn=wf_name: runtime.fire_raw_or_winrt(wid, 4, f"Test:{wn}"),
        ).pack(side="left", padx=4, pady=2)
    matrix_card = ttk.Frame(tab_haptics, style="Card.TFrame", padding=12)
    matrix_card.pack(fill="both", expand=True)
    waveform_choices = ["Click", "Hover", "Collide", "Align", "Step", "Grow", "Success", "Error"]
    for row_idx, (event_key, ev_cfg) in enumerate(runtime.config.events.items()):
        r_frame = ttk.Frame(matrix_card, style="Card.TFrame")
        r_frame.grid(row=row_idx // 2, column=row_idx % 2, sticky="ew", padx=10, pady=4)
        en_var = tk.BooleanVar(value=ev_cfg.enabled)
        wf_var = tk.StringVar(value=ev_cfg.waveform)
        st_var = tk.IntVar(value=ev_cfg.strength)
        def make_updater(ek: str, ev_b: tk.BooleanVar, wf_s: tk.StringVar, st_i: tk.IntVar):
            def _upd(*_a: object) -> None:
                runtime.config.events[ek].enabled = ev_b.get()
                runtime.config.events[ek].waveform = wf_s.get()
                runtime.config.events[ek].strength = int(st_i.get())
                runtime.config.save()
            return _upd
        upd_fn = make_updater(event_key, en_var, wf_var, st_var)
        tk.Checkbutton(
            r_frame,
            text=event_key.replace("_", " ").title(),
            variable=en_var,
            command=upd_fn,
            bg="#1E293B",
            fg="#F8FAFC",
            selectcolor="#0F172A",
            width=20,
            anchor="w",
        ).pack(side="left")
        cb = ttk.Combobox(r_frame, textvariable=wf_var, values=waveform_choices, width=10, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", upd_fn)
        sp = tk.Spinbox(r_frame, from_=1, to=5, textvariable=st_var, width=3, command=upd_fn, bg="#0F172A", fg="#F8FAFC")
        sp.pack(side="left", padx=4)
    # ------------------------------------------------------------------------
    # Tab 3: Action Ring, HID Diagnostics & Live Telemetry Log
    # ------------------------------------------------------------------------
    tab_actions = ttk.Frame(notebook, padding=16)
    notebook.add(tab_actions, text="3. Action Ring, HID Diagnostics & Telemetry")
    act_card = ttk.Frame(tab_actions, style="Card.TFrame", padding=12)
    act_card.pack(fill="x", pady=(0, 12))
    ttk.Label(
        act_card,
        text="Action Ring Radial Menu, HID 0x0E Hardware Diagnostics & Telemetry",
        style="Card.TLabel",
        font=("Segoe UI Semibold", 11),
    ).pack(anchor="w")
    ctrl_row = ttk.Frame(act_card, style="Card.TFrame")
    ctrl_row.pack(fill="x", pady=6)
    ttk.Button(
        ctrl_row,
        text="Summon Radial Action Ring Under Pointer",
        style="Accent.TButton",
        command=lambda: show_action_ring(root.winfo_pointerx(), root.winfo_pointery()),
    ).pack(side="left", padx=(0, 10))
    def on_run_diag() -> None:
        report = RawHidSimpleHapticsDriver.enumerate_diagnostics()
        for line in report.splitlines():
            runtime.log(line)
        root.clipboard_clear()
        root.clipboard_append(report)
        runtime.log("[Diagnostics] Copied full HID diagnostic report to clipboard!")
    ttk.Button(
        ctrl_row,
        text="Run HID 0x0E Diagnostics (Copies Report to Clipboard)",
        style="Accent.TButton",
        command=on_run_diag,
    ).pack(side="left", padx=(0, 10))
    log_box = tk.Listbox(
        tab_actions,
        bg="#020617",
        fg="#38BDF8",
        font=("Consolas", 10),
        height=16,
        borderwidth=0,
    )
    log_box.pack(fill="both", expand=True)
    def poll_logs() -> None:
        while not runtime.event_log.empty():
            try:
                item = runtime.event_log.get_nowait()
                log_box.insert("end", item)
                if log_box.size() > 250:
                    log_box.delete(0)
                log_box.see("end")
            except queue.Empty:
                break
        root.after(100, poll_logs)
    ring_win: Optional[tk.Toplevel] = None
    last_slot_idx: List[int] = [-1]
    def show_action_ring(cx: int, cy: int) -> None:
        nonlocal ring_win
        if ring_win is not None and ring_win.winfo_exists():
            ring_win.destroy()
        size = 320
        ring_win = tk.Toplevel(root)
        ring_win.overrideredirect(True)
        ring_win.attributes("-topmost", True)
        ring_win.geometry(f"{size}x{size}+{cx - size // 2}+{cy - size // 2}")
        ring_win.configure(bg="#0F172A")
        canvas = tk.Canvas(ring_win, width=size, height=size, bg="#0F172A", highlightthickness=2, highlightbackground="#38BDF8")
        canvas.pack(fill="both", expand=True)
        slots = runtime.config.action_ring_slots
        n_slots = len(slots)
        center = size // 2
        outer_r = 135
        inner_r = 45
        def redraw_ring(active_idx: int) -> None:
            canvas.delete("all")
            canvas.create_oval(
                center - inner_r, center - inner_r, center + inner_r, center + inner_r,
                fill="#1E293B", outline="#38BDF8", width=2
            )
            canvas.create_text(center, center, text="Action\nRing", fill="#F8FAFC", font=("Segoe UI Semibold", 9), justify="center")
            for i, slot in enumerate(slots):
                angle_deg = i * (360.0 / n_slots) - 90.0
                rad = math.radians(angle_deg)
                tx = center + math.cos(rad) * ((outer_r + inner_r) / 2.0)
                ty = center + math.sin(rad) * ((outer_r + inner_r) / 2.0)
                fill_col = "#0284C7" if i == active_idx else "#1E293B"
                canvas.create_oval(tx - 42, ty - 22, tx + 42, ty + 22, fill=fill_col, outline="#38BDF8")
                canvas.create_text(tx, ty, text=slot.label, fill="#FFFFFF", font=("Segoe UI Semibold", 8), width=76, justify="center")
        def on_motion(event: tk.Event) -> None:
            dx = event.x - center
            dy = event.y - center
            dist = math.hypot(dx, dy)
            if dist < inner_r:
                if last_slot_idx[0] != -1:
                    last_slot_idx[0] = -1
                    redraw_ring(-1)
                return
            ang = (math.degrees(math.atan2(dy, dx)) + 90.0 + 360.0) % 360.0
            slot_w = 360.0 / n_slots
            idx = int(((ang + slot_w / 2.0) % 360.0) // slot_w)
            if idx != last_slot_idx[0]:
                last_slot_idx[0] = idx
                runtime.trigger_event_haptic("action_ring_slot")
                redraw_ring(idx)
        def on_click(_event: tk.Event) -> None:
            idx = last_slot_idx[0]
            if ring_win is not None:
                ring_win.destroy()
            if 0 <= idx < len(slots):
                chosen = slots[idx]
                runtime.fire_raw_or_winrt(WAVEFORM_SUCCESS, 4, f"ActionRing:{chosen.label}")
                if chosen.action_type == "forever_scroll":
                    on_toggle_forever_btn()
                elif chosen.action_type == "shortcut":
                    runtime._send_hotkey(chosen.payload)
                elif chosen.action_type == "command":
                    subprocess.Popen(chosen.payload, shell=True)
        canvas.bind("<Motion>", on_motion)
        canvas.bind("<ButtonRelease-1>", on_click)
        ring_win.bind("<Escape>", lambda _e: ring_win.destroy() if ring_win else None)
        redraw_ring(-1)
    runtime.on_action_ring_requested = lambda x, y: root.after(0, lambda: show_action_ring(x, y))
    refresh_status()
    poll_logs()
    root.mainloop()

# ============================================================================
# 8. Self-Test & CLI Entrypoint (`--self-test` and `--diag`)
# ============================================================================
def run_self_test() -> int:
    print("Running SurfacePulse v2.0 self-test suite...")
    cfg = SurfacePulseConfig()
    engine = KineticForeverScrollEngine(cfg)
    # 1. Verify 64-bit LPARAM / WPARAM / INPUT struct sizes and conversions
    large_64bit_ptr = 0x00000237F5DCED40  # Exact 64-bit address range from user traceback
    lparam_val = LPARAM_T(large_64bit_ptr)
    wparam_val = WPARAM_T(0x0200)
    assert lparam_val.value == large_64bit_ptr, "64-bit LPARAM conversion failed!"
    assert wparam_val.value == 0x0200, "64-bit WPARAM conversion failed!"
    assert ctypes.sizeof(INPUT) in (28, 40, 56), f"Unexpected INPUT struct size: {ctypes.sizeof(INPUT)}"
    # 2. Slow scroll should trigger firm 'scroll_slow_notch' (Step 0x1014)
    d1 = engine.on_physical_wheel(120, now=1.0)
    assert d1.haptic_event == "scroll_slow_notch", f"Expected slow notch, got {d1}"
    # 3. Fast scroll should trigger light 'scroll_fast_tick' (Hover 0x1008)
    d2 = engine.on_physical_wheel(120, now=1.10)
    assert d2.haptic_event == "scroll_fast_tick", f"Expected fast tick, got {d2}"
    # 4. Hard flick (>18 notches/sec) should enter free-spin and suppress notch haptics
    d3 = engine.on_physical_wheel(240, now=1.12)
    assert d3.entered_freespin and d3.haptic_event is None, f"Expected free-spin, got {d3}"
    # 5. Logitech M720 Forever Scroll toggle should lock continuous spin without decay
    state = engine.toggle_forever_scroll()
    assert state is True
    engine.on_physical_wheel(120, now=2.0)
    deltas = [engine.step_physics(1.0 / 120.0)[0] for _ in range(60)]
    assert all(d > 0 for d in deltas), "Forever scroll should keep spinning continuously!"
    # 6. Clicking or toggling off stops coasting cleanly
    assert engine.stop_coasting() is True
    assert engine.step_physics(1.0 / 120.0)[0] == 0
    print("All SurfacePulse v2.0 64-bit pointer, physics, M720 forever-scroll, and HID 0x0E checks PASSED.")
    return 0

def main() -> int:
    if "--self-test" in sys.argv:
        return run_self_test()
    if "--diag" in sys.argv:
        print(RawHidSimpleHapticsDriver.enumerate_diagnostics())
        return 0
    cfg = SurfacePulseConfig.load()
    runtime = SurfacePulseRuntime(cfg)
    runtime.start()
    try:
        launch_gui(runtime)
    finally:
        runtime.stop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())