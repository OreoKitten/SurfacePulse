# SurfacePulse

> **Disclaimer:** This project is an independent, open-source utility developed for the Microsoft Surface Mouse. It is not affiliated, associated, authorized, endorsed by, or in any way officially connected with Microsoft Corporation or any of its subsidiaries. "Surface" and "Microsoft" are registered trademarks of Microsoft Corporation.

SurfacePulse is a user-mode Windows utility that unlocks hardware haptic feedback, customizable scroll physics, and gesture controls for the Microsoft Surface Mouse (2nd Edition) and compliant HID Simple Haptic Controller devices (Usage Page `0x0E`, Usage `0x01`).

---

## Features

### 1. Direct Hardware Haptics (Usage Page 0x0E)
* Communicates directly with the mouse's top-level haptics collection via standard Win32 SetupAPI and HID APIs without requiring third-party kernel drivers.
* Supports all built-in firmware waveforms: Click, Hover, Step, Align, Grow, Collide, Success, and Error.
* Full 1 to 5 intensity scaling mapped to hardware logical intensity levels.

### 2. Logitech M720-Style "Forever Scroll" and Kinetic Physics
* **Forever Scroll Mode:** Toggles continuous, frictionless scrolling coasting with a single click or an intentional high-speed wheel flick. Coasting continues indefinitely until you click or scroll in reverse.
* **Dynamic Ratchet:** Automatically switches from discrete tactile notches (Step waveform) at slow speeds to light continuous ticks (Hover waveform) during rapid scrolling.
* **Click-to-Stop:** Automatically halts coasting when any mouse button is pressed.

### 3. Desktop UI Event Haptics
Passively monitors desktop interactions to deliver contextual tactile feedback:
* Window snapping and screen edge boundaries.
* Text selection inside editors and browsers (accumulated distance triggers).
* Drag-and-drop pickup and release.
* Hovering over links, buttons, tab controls, and context menus.

### 4. Radial Action Ring and Button Remapping
* Summon a radial overlay menu directly under the cursor for rapid access to shortcuts, Task View, screenshot capture, or forever-scroll toggles.
* Action button support: map single-press, double-press, and hold-drag gestures (such as vertical drag for volume or horizontal drag for screen brightness).

### 5. 64-Bit Safe, Non-Blocking Architecture
* Fully compatible with 64-bit Windows (x64 and ARM64), using explicit pointer type declarations to prevent `LPARAM` overflow errors in low-level mouse hooks.
* Passive hook design: input events are placed onto a background thread queue so HID output reports never cause cursor lag or input drops.
* Touchscreen and Surface Pen bypass filters to avoid interfering with digitizer inputs.

---

## Requirements

* Windows 10 or Windows 11 (x64 or ARM64)
* Python 3.8 or newer
* Standard Python installation with `tkinter` enabled (bundled by default with official Windows Python installers)
* Microsoft Surface Mouse (2nd Edition) or any mouse exposing HID Usage Page `0x0E`

---

## Installation and Quick Start

1. Clone or download this repository:
   ```cmd
   git clone https://github.com/your-username/surfacepulse.git
   cd surfacepulse
   ```

2. Run the hardware diagnostic tool to verify that your mouse exposes the haptic controller interface:
   ```cmd
   python surface_mouse_haptics_win.py --diag
   ```

3. Launch the application and graphical control panel:
   ```cmd
   python surface_mouse_haptics_win.py
   ```

---

## Command-Line Arguments

SurfacePulse includes dedicated flags for diagnostics and testing:

* `python surface_mouse_haptics_win.py`
  Runs the main runtime daemon and opens the configuration GUI.

* `python surface_mouse_haptics_win.py --diag`
  Inspects all connected Microsoft (`VID_045E`) and Haptics (`UsagePage 0x0E`) collections, attempts a test pulse, and outputs a diagnostic log. No mouse hooks are installed in this mode.

* `python surface_mouse_haptics_win.py --self-test`
  Runs the internal validation suite for 64-bit structure sizes, scroll physics velocity calculations, and threshold evaluations.

---

## Configuration

Settings are saved automatically upon change to a local JSON file in your Windows roaming user profile:

```text
%APPDATA%\SurfacePulse\config.json
```
(Typically: `C:\Users\<Username>\AppData\Roaming\SurfacePulse\config.json`)

To reset all options to factory defaults:
1. Close SurfacePulse.
2. Delete the `config.json` file from `%APPDATA%\SurfacePulse\`.
3. Re-launch the program to regenerate the default configuration.

### Default Event Haptic Strengths

| Event | Waveform | Strength (1 to 5) |
| :--- | :--- | :---: |
| Left Click | Click | 3 |
| Right Click | Click | 2 |
| Scroll Slow Notch | Step | 4 |
| Scroll Fast Tick | Hover | 2 |
| Scroll Freespin Stop | Collide | 2 |
| Forever Scroll Toggle | Success | 4 |
| Hover Button / Tab / Link | Hover | 2 |
| Hover Menu Item | Align | 2 |
| Slider Step | Step | 3 |
| Text Selection | Hover | 1 |
| Window Snap | Align | 5 |
| Drag Pickup | Grow | 3 |
| Drag Drop | Success | 4 |
| Screen Edge | Collide | 3 |
| Action Ring Slot | Align | 3 |
| Force Click | Grow | 5 |
| Gesture Step | Step | 3 |

---

## Troubleshooting

### Device shows "Standby Mode" or haptics do not fire
1. Ensure your Surface Mouse is actively paired and connected over Bluetooth.
2. If the mouse was in low-power sleep, move the wheel to wake it; the background worker will automatically reconnect.
3. Run `python surface_mouse_haptics_win.py --diag` to check if Windows grants read/write permissions to the `0x0E` collection.

### Scrolling feels too sensitive or heavy
1. Open the "Forever Scroll (M720) & Wheel Ratchet" tab in the GUI.
2. Adjust the "Slow -> Fast Ratchet Threshold" to change when discrete notches transition to light ticks.
3. Adjust "Kinetic Momentum Coast" to decrease or increase friction during flick-scrolling.

---

## License

This project is released under the MIT License. See `LICENSE` for details.
