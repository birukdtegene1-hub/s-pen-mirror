# S-Pen Mirror

Use a Galaxy Note20 Ultra as a live writing surface on Garuda Hyprland, or share the whole phone display. The Android app sends pictures directly to a Qt desktop window over your local network. The PC can use Ethernet while the phone uses Wi-Fi on the same LAN.

## First use

1. Copy [S-Pen-Mirror.apk](dist/S-Pen-Mirror.apk) to the phone, for example with USB file transfer or KDE Connect. Open it in **My Files** and tap **Install**. If Samsung asks, allow My Files to install this app.
2. Open a terminal in the project folder on the PC. Run the receiver to display its pairing QR code.

   ```sh
   ./s-pen-mirror
   ```

3. Open **S-Pen Mirror** on the phone. Tap **Pair PC**, allow camera access, and scan the code in the desktop window. The status changes to **Paired** and the PC displays your ink canvas.

Run the same PC command for future sessions. Each launch creates a fresh pairing code. To pair without the camera, choose **Copy pairing link** on the PC, transfer the link to the phone, and choose **Enter pairing link** on the scanner screen.

## Using the pen

- **Ink canvas** starts with S-Pen input and ignores finger touches on the page. Finger touches still operate the toolbar. Use **More → Allow finger drawing** to draw with a finger too.
- Tap the brush name to select a pressure-sensitive ink pen, fixed-width ballpoint, translucent highlighter, or eraser. **Color** has a palette and hex input. Tap the **px** control for stroke width. Starting a stroke with the S-Pen side button held uses the eraser when the device delivers that button event.
- **Paper** offers plain, ruled, and grid sheets. **Rotate page 90°** rotates the page and its ink. Turn the phone sideways for a wider drawing surface. Page proportions stay consistent on both displays.
- **Undo**, **Redo**, and **More → Clear canvas** work live. The app saves the last canvas on the phone after you finish writing. Undo history covers the most recent 64 edits in the current app process.
- The PC shows just the page as you write. **Save PNG** exports the current page at its original resolution. **F11** enters full screen and **Esc** returns to the window.

## Mirroring the phone display

Tap **Screen mirror** and accept Android’s screen-sharing prompt. You can now open another app or turn the phone sideways. **Ink canvas**, **Stop sharing**, or the notification’s stop action ends capture and returns to the drawing. A disconnected session also ends capture.

Screen mirroring is view-only video, up to 1280 pixels on the long edge and about 15 frames per second. The stream contains no audio. Android can blank protected content, including some streaming and banking screens. [Android screen capture](https://developer.android.com/media/grow/media-projection)

## Connection help

- **Wrong address in the PC window.** Run `ip -br -4 addr` and find the address of the Wi-Fi or Ethernet adapter the phone can reach. For example, if that address is `192.168.1.25`, run `./s-pen-mirror --host 192.168.1.25`.
- **PC unreachable.** Make sure both devices can reach each other on the LAN. Guest Wi-Fi can isolate clients. If the Garuda firewall filters inbound traffic, allow **TCP 47831** from the phone on the local network. Use `--port 47832` to choose another port if the default is occupied.
- **Wi-Fi dropped.** Your canvas stays on the phone. Tap **Pair PC → Reconnect** while the same PC window is running. If you restarted the receiver, choose **Scan a new code**.
- **Missing PC dependencies.** The receiver uses Python 3, PySide6, OpenSSL, and qrencode. The Garuda package command is `sudo pacman -S --needed python pyside6 openssl qrencode`.

## Source and build

The included APK supports Android 10 and newer. Installing it requires no build tools. For a rebuild on Linux, use a JDK, the Android SDK platform 35 and Build Tools 35.0.0, plus `curl`, `openssl`, `flock`, and `zip`. Set `ANDROID_HOME` to the SDK directory and run `./android/build.sh`. The script verifies the pinned ZXing QR decoder dependency, compiles and signs the app, and verifies the output in `dist/`.

On the first build, the script creates a local signing key in the ignored `android/local-signing/` directory. Keep that directory private and retain it to sign future updates to your build.

The PC binds the selected IPv4 interface. Pairing uses an ephemeral TLS certificate pinned by the QR code and a random session token. One phone can publish at a time. Ink sends a full lossless page at pairing and after page-wide edits, then lossless patches during writing. The screen mode sends JPEG frames. Both streams use bounded queues so a slow connection can catch up.

Run `python3 -m unittest discover -s tests -v` for the desktop protocol tests.
