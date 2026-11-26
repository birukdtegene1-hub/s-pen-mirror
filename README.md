# S-Pen Mirror

Use a Galaxy Note20 Ultra to see and annotate a portion of your Garuda Hyprland desktop. You can also share the phone’s ink canvas or mirror its display on the PC. The PC can use Ethernet while the phone uses Wi-Fi on the same LAN.

## First use

1. Copy [S-Pen-Mirror.apk](dist/S-Pen-Mirror.apk) to the phone, for example with USB file transfer or KDE Connect. Open it in **My Files** and tap **Install**. If Samsung asks, allow My Files to install this app.
2. Click the pen on Waybar beside the system tray or in the expanded Garuda Notch to open the pairing QR code. To add the buttons, run the one-time setup for each bar you use from the project folder on the PC.

   ```sh
   ./install-waybar
   ./install-notch
   ```

3. Open **S-Pen Mirror** on the phone. Tap **Pair PC**, allow camera access, and scan the code in the desktop window. The status changes to **Paired** and the PC displays your ink canvas.

The pen turns green when the receiver is ready. Click either button again to stop the receiver and end the phone connection. Both bars control the same session. You can also launch the receiver directly with `./s-pen-mirror`. Each launch creates a fresh pairing code. To pair without the camera, choose **Copy pairing link** on the PC, transfer the link to the phone, and choose **Enter pairing link** on the scanner screen. Copied links expire after 90 seconds. The current app exchanges the copied link for an in-memory reconnect credential on first use.

## Using the pen

- **Ink canvas** starts with S-Pen input and ignores finger touches on the page. Finger touches still operate the toolbar. Use **More → Allow finger drawing** to draw with a finger too.
- Tap the brush name to select a pressure-sensitive ink pen, fixed-width ballpoint, translucent highlighter, or eraser. **Color** has a palette and hex input. Tap the **px** control for stroke width.
- Tap the S-Pen side button near or against the phone screen to switch to the eraser. Tap again to restore the selected pen. The ring shows the eraser position and changes with its size. Choose **Area eraser** to remove only swept ink or **Stroke eraser** to remove an entire touched stroke. Use Area eraser for marks loaded into the phone’s ink canvas as a bitmap.
- **Paper** offers plain, ruled, and grid sheets. **Rotate page 90°** rotates the page and its ink. Turn the phone sideways for a wider drawing surface. Page proportions stay consistent on both displays.
- **Undo**, **Redo**, and **More → Clear canvas** work live. The app saves the last canvas on the phone after you finish writing. Undo history covers the most recent 64 edits in the current app process.
- On the phone, **More → Full screen** hides the bars and tools. Tap **Exit** or use Android Back to restore them.
- The PC shows just the page as you write. **Save PNG** exports the current page at its original resolution. **F11** enters full screen and **Esc** returns to the window.

## Annotating PDFs

1. Pair the phone, then click **Open PDF** in the S-Pen Mirror desktop window and choose the file to edit.
2. Tap **Write on PC** on the phone. Use the S-Pen to annotate the live page and your fingers to control the PC pointer.
3. Scroll with two fingers while the PC pointer is over the page to change slides. You can also click **Previous** or **Next** on the PC or use the arrow or page keys. Each page keeps its own ink.

Writable PDFs save automatically after a short pause. **Save PDF** saves immediately. Use **Save a copy** for a signed or read-only PDF, or when a second app has changed the file on disk. Ink is saved as editable PDF annotations. The erasers can edit native ink and imported PDFs may contain marks flattened into the page.

## Writing on the PC screen

1. Pair the phone and tap **Write on PC**.
2. On the PC, click **Select region** in S-Pen Mirror and drag a rectangle within one display. The selected area appears live on the phone. Press **Esc** to cancel the picker.
3. Write with the S-Pen directly on the preview. The marks appear as an overlay on the PC and in the phone’s preview. Turn the phone sideways to use the same region in landscape.
4. Use **+** and **−** to zoom. **Fit** shows the entire region, and **Fill** zooms to fill the phone’s writing area. Select **More → Use fingers to zoom and pan** to pinch and drag the preview. The same menu switches back to touchpad mode.
5. Tap **Full screen** beside the zoom controls to give the preview the whole phone screen. Writing and the selected finger mode stay active. Tap **Exit** or use Android Back to restore the controls.

Use the pen, ballpoint, highlighter, eraser, undo, redo, and clear controls for the desktop overlay. Each active window, workspace, display, and full-screen state has its own canvas. The receiver caches up to 16 recent canvases and keeps up to 64 edits per canvas within a shared memory budget. If focus moves to another display, the preview pauses until you return to the selected display.

Open S-Pen Mirror from the PC’s system tray to **Change region** or **Stop casting**. On desktops without a tray, use the receiver window. Tap **Stop writing** on the phone to end the cast and return to the ink canvas.

### Phone touchpad

- Slide one finger to move the PC cursor. Tap to click, double-tap to double-click, or double-tap and slide on the second touch to drag.
- Slide two fingers to scroll vertically or horizontally. Tap with two fingers to right-click or three fingers to middle-click.
- Swipe left or right with three fingers to focus the next display in that direction. On a single display the swipe changes workspaces. The screen preview follows and restores your selected region when you return to its display. A shared PDF stays on the phone.
- Spread or pinch two fingers to adjust Hyprland's desktop magnifier around the PC cursor. The original zoom setting returns when you stop sharing. For preview-only zoom, use **+** and **−** or **More → Use fingers to zoom and pan**.
- Touchpad input is available while a PC region or PDF is shared. It uses Hyprland’s Wayland virtual pointer. When the compositor cannot provide it, fingers zoom and pan the preview.

## Mirroring the phone display

Tap **Screen mirror** and accept Android’s screen-sharing prompt. You can now open another app or turn the phone sideways. **Ink canvas**, **Stop sharing**, or the notification’s stop action ends capture and returns to the drawing. A disconnected session also ends capture.

Screen mirroring is view-only video, up to 1280 pixels on the long edge and about 15 frames per second. The stream contains no audio. Android can blank protected content, including some streaming and banking screens. [Android screen capture](https://developer.android.com/media/grow/media-projection)

## Connection help

- **Wrong address in the PC window.** Run `ip -br -4 addr` and find the address of the Wi-Fi or Ethernet adapter the phone can reach. For example, if that address is `192.168.1.25`, run `./s-pen-mirror --host 192.168.1.25`.
- **PC unreachable.** Make sure both devices can reach each other on the LAN. Guest Wi-Fi can isolate clients. If the Garuda firewall filters inbound traffic, allow **TCP 47831** from the phone on the local network. Use `--port 47832` to choose another port if the default is occupied.
- **Wi-Fi dropped.** Your canvas stays on the phone. Tap **Pair PC → Reconnect** while the same PC window is running. If you restarted the receiver, choose **Scan a new code**.
- **Missing PC dependencies.** Install the receiver, Wayland casting, and PDF dependencies on Garuda with `sudo pacman -S --needed python pyside6 openssl qrencode grim slurp gtk-layer-shell python-gobject python-cairo poppler-glib`. PDF annotation requires Poppler GLib 25.06 or newer.

## Source and build

The included APK supports Android 10 and newer. Installing it requires no build tools. For a rebuild on Linux, use a JDK, the Android SDK platform 35 and Build Tools 35.0.0, plus `curl`, `openssl`, `flock`, and `zip`. Set `ANDROID_HOME` to the SDK directory and run `./android/build.sh`. The script verifies the pinned ZXing QR decoder dependency, compiles and signs the app, and verifies the output in `dist/`.

On the first build, the script creates a local signing key in the ignored `android/local-signing/` directory. Keep that directory private and retain it to sign future updates to your build.

The PC binds the selected IPv4 interface. Pairing uses TLS 1.3, a certificate pinned by the QR code, and a random session token. Restart the receiver if its seven-day certificate has expired. One phone can pair at a time. Local ink sends a full lossless page at pairing and after page-wide edits, then lossless patches while drawing. Both directions of screen sharing use JPEG frames. The PC captures the region up to 12 times per second and backs off to 6 when the image stays still. Unchanged frames reuse the phone’s last image. Pressure-aware S-Pen events return through the same TLS connection.

Run `python3 -m unittest discover -s tests -v` for the desktop protocol and rendering tests.
