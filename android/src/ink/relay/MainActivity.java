package ink.relay;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.media.projection.MediaProjectionConfig;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;
import java.util.Locale;

public final class MainActivity extends Activity {
    private static final int SCAN = 10, CAPTURE = 11, NOTIFICATIONS = 12;
    private static final String[] TOOLS = {"Ink pen", "Ballpoint", "Highlighter", "Area eraser", "Stroke eraser"};
    private static final String[] COLORS = {"Forest ink", "Blue", "Green", "Coral", "Purple", "Gold", "Black", "White"};
    private RelayApp app;
    private Link link;
    private PadView pad;
    private LinearLayout header, modes, dock, mirrorPanel, zoomControls;
    private TextView status, castHint, eraserBadge;
    private Button enterFullScreen, exitFullScreen;
    private Button connect, inkMode, screenMode, writeMode, tool, color, size, undo, redo, paper;
    private boolean capturePrompt, wasCasting, fullScreen, panWithFingers;
    private Runnable releaseFullscreenBack;
    private final CastViewport viewport = new CastViewport();
    private final Runnable linkChanged = this::refreshLink;
    private final Runnable frameChanged = this::refreshPreview;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        app = (RelayApp) getApplication();
        app.attachMain();
        link = app.link;
        fullScreen = state != null && state.getBoolean("fullScreen") && !link.screen;
        panWithFingers = state != null && state.getBoolean("panWithFingers");
        getWindow().setStatusBarColor(Ui.SHELL);
        getWindow().setNavigationBarColor(Ui.SHELL);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        build();
        link.listen(linkChanged);
        link.listenFrames(frameChanged);
        refreshLink();
        applySystemBars();
        updateBackHandling();
    }

    private void build() {
        boolean wide = getResources().getConfiguration().orientation == Configuration.ORIENTATION_LANDSCAPE;
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Ui.SHELL);
        header = Ui.row(this);
        Ui.pad(header, 10, wide ? 2 : 5);
        LinearLayout title = new LinearLayout(this);
        title.setOrientation(LinearLayout.VERTICAL);
        title.addView(Ui.text(this, "S-Pen Mirror", wide ? 18 : 21, Ui.INK, true));
        status = Ui.text(this, "", 12, Ui.MUTED, false);
        title.addView(status);
        header.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        modes = Ui.row(this);
        inkMode = Ui.button(this, "Ink canvas", this::showInk);
        screenMode = Ui.button(this, "Screen mirror", this::startMirror);
        writeMode = Ui.button(this, "Write on PC", this::toggleWriting);
        Ui.item(modes, inkMode, wide ? 0 : 1, wide ? 42 : 44);
        Ui.item(modes, writeMode, wide ? 0 : 1, wide ? 42 : 44);
        Ui.item(modes, screenMode, wide ? 0 : 1, wide ? 42 : 44);
        if (wide) header.addView(modes);
        connect = Ui.button(this, "Pair PC", this::pair);
        Ui.item(header, connect, 0, 44);
        root.addView(header);
        if (!wide) {
            Ui.pad(modes, 8, 2);
            root.addView(modes);
        }
        FrameLayout area = new FrameLayout(this);
        pad = new PadView(this, viewport, this::refreshTools);
        pad.setPanWithFingers(panWithFingers);
        area.addView(pad);
        castHint = Ui.text(this, "", 16, 0xffeef2eb, false);
        castHint.setBackgroundColor(0xcc202725);
        castHint.setGravity(Gravity.CENTER);
        Ui.pad(castHint, 24, 4);
        area.addView(castHint, new FrameLayout.LayoutParams(-1, -2, Gravity.CENTER));
        mirrorPanel = new LinearLayout(this);
        mirrorPanel.setOrientation(LinearLayout.VERTICAL);
        mirrorPanel.setGravity(Gravity.CENTER);
        mirrorPanel.setBackgroundColor(0xffe7eee5);
        Ui.pad(mirrorPanel, 24, 14);
        mirrorPanel.addView(Ui.text(this, "Sharing your screen", 27, Ui.INK, true));
        TextView hint = Ui.text(this, "Open any app to show it on your PC.", 16, Ui.MUTED, false);
        Ui.pad(hint, 0, 12);
        mirrorPanel.addView(hint);
        Button stop = Ui.button(this, "Stop sharing", this::showInk);
        Ui.tint(stop, true);
        mirrorPanel.addView(stop, new LinearLayout.LayoutParams(Ui.dp(this, 150), Ui.dp(this, 48)));
        area.addView(mirrorPanel);
        eraserBadge = Ui.text(this, "Eraser", 14, Color.WHITE, true);
        eraserBadge.setGravity(Gravity.CENTER);
        eraserBadge.setBackground(Ui.shape(this, Ui.INK, 8));
        Ui.pad(eraserBadge, 12, 8);
        FrameLayout.LayoutParams badgePosition = new FrameLayout.LayoutParams(-2, -2, Gravity.TOP | Gravity.START);
        badgePosition.setMargins(Ui.dp(this, 8), Ui.dp(this, 8), Ui.dp(this, 8), 0);
        area.addView(eraserBadge, badgePosition);
        exitFullScreen = Ui.button(this, "Exit", () -> setFullScreen(false));
        exitFullScreen.setContentDescription("Exit full screen");
        FrameLayout.LayoutParams exitPosition = new FrameLayout.LayoutParams(-2, Ui.dp(this, 48), Gravity.TOP | Gravity.END);
        exitPosition.setMargins(Ui.dp(this, 8), Ui.dp(this, 8), Ui.dp(this, 8), 0);
        area.addView(exitFullScreen, exitPosition);
        root.addView(area, new LinearLayout.LayoutParams(-1, 0, 1));
        dock = new LinearLayout(this);
        dock.setOrientation(LinearLayout.VERTICAL);
        Ui.pad(dock, 6, 3);
        zoomControls = Ui.row(this);
        Button zoomOut = Ui.button(this, "−", () -> pad.adjustPreview(() -> viewport.zoomBy(.8f)));
        zoomOut.setContentDescription("Zoom out");
        Button zoomIn = Ui.button(this, "+", () -> pad.adjustPreview(() -> viewport.zoomBy(1.25f)));
        zoomIn.setContentDescription("Zoom in");
        Ui.item(zoomControls, zoomOut, 1, 40);
        Ui.item(zoomControls, Ui.button(this, "Fit", () -> pad.adjustPreview(viewport::fit)), 1.2f, 40);
        Ui.item(zoomControls, Ui.button(this, "Fill", () -> pad.adjustPreview(viewport::fill)), 1.2f, 40);
        Ui.item(zoomControls, zoomIn, 1, 40);
        enterFullScreen = Ui.button(this, "Full screen", () -> setFullScreen(true));
        Ui.item(zoomControls, enterFullScreen, 2.1f, 40);
        dock.addView(zoomControls);
        LinearLayout brushes = Ui.row(this);
        tool = Ui.button(this, "Ink pen", this::chooseTool);
        color = Ui.button(this, "Color", this::chooseColor);
        size = Ui.button(this, "Size", this::chooseSize);
        Ui.item(brushes, tool, 1.35f, 44);
        Ui.item(brushes, color, 1, 44);
        Ui.item(brushes, size, 1, 44);
        dock.addView(brushes);
        LinearLayout edits = wide ? brushes : Ui.row(this);
        undo = Ui.button(this, "Undo", () -> step(false));
        redo = Ui.button(this, "Redo", () -> step(true));
        Ui.item(edits, undo, 1, 44);
        Ui.item(edits, redo, 1, 44);
        paper = Ui.button(this, "Paper", this::choosePaper);
        Ui.item(edits, paper, 1, 44);
        Ui.item(edits, Ui.button(this, "More", this::more), 1, 44);
        if (!wide) dock.addView(edits);
        root.addView(dock);
        setContentView(root);
    }

    @Override public void onConfigurationChanged(Configuration config) {
        super.onConfigurationChanged(config);
        pad.cancelStroke();
        build();
        refreshLink();
    }

    @Override protected void onResume() { super.onResume(); refreshLink(); }
    @Override protected void onPause() {
        pad.cancelFingerInput();
        pad.clearStylus();
        if (pad.page != null) {
            pad.cancelStroke();
            pad.page.save();
        }
        super.onPause();
    }
    @Override protected void onDestroy() {
        if (releaseFullscreenBack != null) releaseFullscreenBack.run();
        link.unlisten(linkChanged);
        link.unlistenFrames(frameChanged);
        app.detachMain(isChangingConfigurations());
        super.onDestroy();
    }
    @Override protected void onSaveInstanceState(Bundle out) {
        out.putBoolean("fullScreen", fullScreen);
        out.putBoolean("panWithFingers", panWithFingers);
        super.onSaveInstanceState(out);
    }

    @Override public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) applySystemBars();
        else if (pad != null) pad.clearStylus();
    }

    @Override public void onBackPressed() {
        if (fullScreen) setFullScreen(false);
        else super.onBackPressed();
    }

    private void setFullScreen(boolean enabled) {
        if (enabled == fullScreen) return;
        pad.cancelStroke();
        pad.cancelFingerInput();
        fullScreen = enabled;
        updatePresentation();
        applySystemBars();
        updateBackHandling();
    }

    private void updateBackHandling() {
        if (Build.VERSION.SDK_INT < 33) return;
        if (fullScreen && releaseFullscreenBack == null) {
            OnBackInvokedDispatcher dispatcher = getOnBackInvokedDispatcher();
            OnBackInvokedCallback callback = () -> setFullScreen(false);
            dispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, callback);
            releaseFullscreenBack = () -> dispatcher.unregisterOnBackInvokedCallback(callback);
        } else if (!fullScreen && releaseFullscreenBack != null) {
            releaseFullscreenBack.run();
            releaseFullscreenBack = null;
        }
    }

    private void updatePresentation() {
        header.setVisibility(fullScreen ? View.GONE : View.VISIBLE);
        modes.setVisibility(fullScreen ? View.GONE : View.VISIBLE);
        dock.setVisibility(fullScreen || link.screen ? View.GONE : View.VISIBLE);
        exitFullScreen.setVisibility(fullScreen ? View.VISIBLE : View.GONE);
        eraserBadge.setVisibility(fullScreen && pad.erasing() ? View.VISIBLE : View.GONE);
    }

    private void applySystemBars() {
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController bars = getWindow().getInsetsController();
            if (bars == null) return;
            int appearance = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                    | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
            bars.setSystemBarsAppearance(appearance, appearance);
            bars.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            if (fullScreen) bars.hide(WindowInsets.Type.systemBars());
            else bars.show(WindowInsets.Type.systemBars());
        } else {
            int flags = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            if (fullScreen) flags |= View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY;
            getWindow().getDecorView().setSystemUiVisibility(flags);
        }
    }

    private void refreshLink() {
        boolean screen = link.screen;
        boolean writing = link.writing;
        boolean casting = link.casting();
        if (screen || (wasCasting && !casting)) setFullScreen(false);
        if (casting != wasCasting) {
            viewport.fit();
            wasCasting = casting;
        }
        status.setText(link.status);
        connect.setText(link.state == Link.State.ONLINE ? "Paired" : link.state == Link.State.CONNECTING ? "Cancel" : "Pair PC");
        writeMode.setText(writing ? "Stop writing" : "Write on PC");
        writeMode.setEnabled(link.state == Link.State.ONLINE && !screen);
        Ui.tint(inkMode, !screen && !writing);
        Ui.tint(screenMode, screen);
        Ui.tint(writeMode, writing);
        mirrorPanel.setVisibility(screen ? View.VISIBLE : View.GONE);
        pad.setVisibility(screen ? View.GONE : View.VISIBLE);
        updatePresentation();
        paper.setVisibility(writing ? View.GONE : View.VISIBLE);
        zoomControls.setVisibility(casting ? View.VISIBLE : View.GONE);
        refreshPreview();
        if (!screen) pad.syncPage();
        refreshTools();
    }

    private void refreshPreview() {
        boolean casting = link.casting();
        Link.CastFrame frame = casting ? link.castFrame : Link.CastFrame.EMPTY;
        pad.showPreview(frame);
        boolean touchpad = frame.touchpad && !panWithFingers;
        String description = !casting ? "S-Pen writing area" : touchpad
                ? "PC preview. Use fingers as a touchpad and write with the S-Pen."
                : "PC preview. Pinch to zoom, drag to pan, and write with the S-Pen.";
        if (touchpad && link.monitorSwipeSupported) {
            description += " Swipe left or right with three fingers to switch displays or workspaces.";
        }
        if (touchpad && link.magnifierSupported) {
            description += " Pinch with two fingers to zoom the PC display.";
        }
        if (!description.contentEquals(pad.getContentDescription())) pad.setContentDescription(description);
        String label = frame.touchpad ? "Write on PC · " + (touchpad ? "Touchpad" : "Zoom and pan") : link.status;
        if (casting && frame.status == Link.CastFrame.READY && !frame.touchpad) {
            label = link.touchpadSupported ? "Touchpad unavailable · Zoom and pan"
                    : "Restart the PC receiver for touchpad";
        } else if (touchpad && !link.magnifierSupported) {
            label = "Touchpad · Restart PC receiver for pinch zoom";
        }
        if (!label.contentEquals(status.getText())) status.setText(label);
        boolean ready = frame.status == Link.CastFrame.READY && frame.bitmap != null;
        castHint.setVisibility(casting && !ready ? View.VISIBLE : View.GONE);
        String hint;
        switch (frame.status) {
            case Link.CastFrame.WAIT: hint = "Starting the PC preview…"; break;
            case Link.CastFrame.ERROR: hint = "Could not capture the region. Try selecting it again on your PC."; break;
            case Link.CastFrame.PAUSED: hint = "Preview paused. Focus the selected display or choose a new region."; break;
            case Link.CastFrame.READY: hint = "Waiting for the PC preview…"; break;
            default: hint = "Choose Select region in S-Pen Mirror on your PC.";
        }
        if (!hint.contentEquals(castHint.getText())) castHint.setText(hint);
        for (int i = 0; i < zoomControls.getChildCount(); i++) {
            View button = zoomControls.getChildAt(i);
            boolean enabled = ready || button == enterFullScreen;
            if (button.isEnabled() != enabled) {
                button.setEnabled(enabled);
                button.setAlpha(enabled ? 1f : .45f);
            }
        }
        if (casting && undo.isEnabled() != ready) refreshTools();
    }

    private void refreshTools() {
        InkPage p = pad.page;
        if (p == null) return;
        boolean erasing = pad.erasing();
        tool.setText(TOOLS[(erasing ? p.eraserTool() : p.tool).ordinal()] + " ▾");
        eraserBadge.setText(TOOLS[p.eraserTool().ordinal()]);
        eraserBadge.setVisibility(fullScreen && erasing ? View.VISIBLE : View.GONE);
        size.setText(erasing ? "Size " + p.eraserSize + " ▾" : p.size + " px ▾");
        GradientDrawable swatch = Ui.shape(this, p.color, 8);
        int d = Ui.dp(this, 16);
        swatch.setBounds(0, 0, d, d);
        swatch.setStroke(Ui.dp(this, 1), 0xffa5b2a6);
        color.setCompoundDrawables(swatch, null, null, null);
        color.setCompoundDrawablePadding(Ui.dp(this, 2));
        color.setContentDescription(String.format(Locale.ROOT, "Pen color #%06X", 0xffffff & p.color));
        boolean desktopReady = !link.casting() || link.castFrame.status == Link.CastFrame.READY;
        undo.setEnabled(link.writing ? desktopReady : !p.undo.isEmpty());
        redo.setEnabled(link.writing ? desktopReady : !p.redo.isEmpty());
        undo.setAlpha(undo.isEnabled() ? 1f : 0.45f);
        redo.setAlpha(redo.isEnabled() ? 1f : 0.45f);
    }

    private void chooseTool() {
        InkPage p = pad.page;
        if (p == null) return;
        int selection = (pad.erasing() ? p.eraserTool() : p.tool).ordinal();
        new AlertDialog.Builder(this).setTitle("Pen tool").setSingleChoiceItems(TOOLS, selection, (dialog, i) -> {
            pad.cancelStroke();
            boolean erase = i >= InkPage.Tool.ERASER.ordinal();
            if (erase) p.strokeEraser = i == InkPage.Tool.STROKE_ERASER.ordinal();
            else p.tool = InkPage.Tool.values()[i];
            pad.selectEraser(erase);
            p.saveSettings();
            refreshTools();
            dialog.dismiss();
        }).setNegativeButton("Cancel", null).show();
    }

    private void chooseColor() {
        InkPage p = pad.page;
        if (p == null) return;
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        Ui.pad(body, 18, 8);
        LinearLayout[] rows = {Ui.row(this), Ui.row(this)};
        for (LinearLayout row : rows) body.addView(row);
        EditText hex = new EditText(this);
        hex.setSingleLine();
        hex.setHint("#RRGGBB");
        hex.setContentDescription("Hex color");
        hex.setText(String.format(Locale.ROOT, "#%06X", 0xffffff & p.color));
        body.addView(hex);
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("Pen color").setView(body)
                .setPositiveButton("Use hex color", null).setNegativeButton("Cancel", null).create();
        for (int i = 0; i < InkPage.COLORS.length; i++) {
            final int value = InkPage.COLORS[i];
            Button sample = Ui.button(this, p.color == value ? "✓" : "", () -> {
                p.color = value; p.saveSettings(); refreshTools(); dialog.dismiss();
            });
            sample.setContentDescription(COLORS[i]);
            sample.setTextColor(Color.luminance(value) > 0.45 ? 0xff172a20 : Color.WHITE);
            GradientDrawable bg = Ui.shape(this, value, 8);
            bg.setStroke(Ui.dp(this, 1), 0xffb2c0b2);
            sample.setBackground(bg);
            Ui.item(rows[i / 4], sample, 1, 44);
        }
        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            String value = hex.getText().toString().trim();
            if (!value.startsWith("#")) value = "#" + value;
            if (!value.matches("#[a-fA-F0-9]{6}")) { hex.setError("Enter six hex digits"); return; }
            p.color = Color.parseColor(value); p.saveSettings(); refreshTools(); dialog.dismiss();
        }));
        dialog.show();
    }

    private void chooseSize() {
        InkPage p = pad.page;
        if (p == null) return;
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        Ui.pad(body, 24, 14);
        boolean erasing = pad.erasing();
        int initial = erasing ? p.eraserSize : p.size;
        String units = erasing ? "" : " px";
        TextView label = Ui.text(this, initial + units, 20, Ui.INK, true);
        body.addView(label);
        SeekBar slider = new SeekBar(this);
        slider.setMax(35);
        slider.setProgress(initial - 1);
        slider.setContentDescription(erasing ? "Eraser size" : "Stroke width");
        slider.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            public void onProgressChanged(SeekBar bar, int value, boolean user) {
                if (erasing) p.eraserSize = value + 1;
                else p.size = value + 1;
                label.setText((value + 1) + units);
                refreshTools();
            }
            public void onStartTrackingTouch(SeekBar bar) { }
            public void onStopTrackingTouch(SeekBar bar) { p.saveSettings(); }
        });
        body.addView(slider, new LinearLayout.LayoutParams(-1, Ui.dp(this, 52)));
        new AlertDialog.Builder(this).setTitle(erasing ? "Eraser size" : "Stroke width").setView(body).setPositiveButton("Done", null)
                .setOnDismissListener(d -> p.saveSettings()).show();
    }

    private void choosePaper() {
        InkPage p = pad.page;
        if (p == null) return;
        new AlertDialog.Builder(this).setTitle("Paper").setSingleChoiceItems(InkPage.PAPERS, p.paper, (d, i) -> {
            p.paper = i; p.saveSettings(); pad.syncPage(); d.dismiss();
        }).setNeutralButton("Rotate page 90°", (d, i) -> pad.rotatePage()).setNegativeButton("Cancel", null).show();
    }

    private void step(boolean forward) {
        if (pad.page == null) return;
        if (link.writing) {
            pad.editDesktop((byte) (forward ? 'R' : 'U'));
            return;
        }
        pad.cancelStroke(); pad.page.step(forward); pad.syncPage(); refreshTools();
    }

    private void more() {
        InkPage p = pad.page;
        if (p == null) return;
        if (link.writing) {
            String[] actions = link.castFrame.touchpad
                    ? new String[]{"Clear annotations", "Full screen", panWithFingers ? "Use fingers as touchpad" : "Use fingers to zoom and pan"}
                    : new String[]{"Clear annotations", "Full screen"};
            new AlertDialog.Builder(this).setTitle("PC screen")
                    .setItems(actions, (d, i) -> {
                        if (i == 0) pad.editDesktop((byte) 'C');
                        else if (i == 1) setFullScreen(true);
                        else {
                            panWithFingers = !panWithFingers;
                            pad.setPanWithFingers(panWithFingers);
                            refreshPreview();
                        }
                    }).show();
            return;
        }
        String[] options = {p.fingers ? "Use S-Pen only" : "Allow finger drawing", "Clear canvas", "Full screen"};
        new AlertDialog.Builder(this).setItems(options, (d, i) -> {
            if (i == 0) { p.fingers = !p.fingers; p.saveSettings(); }
            else if (i == 2) setFullScreen(true);
            else new AlertDialog.Builder(this).setTitle("Clear this canvas?").setMessage("You can undo this change.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Clear", (confirm, which) -> {
                        pad.cancelStroke(); p.clear(); pad.syncPage(); refreshTools();
                    }).show();
        }).show();
    }

    private void pair() {
        if (link.state == Link.State.CONNECTING) { link.disconnect(); return; }
        if (link.state == Link.State.ONLINE) {
            new AlertDialog.Builder(this).setTitle("Paired with " + link.endpoint.host)
                    .setNegativeButton("Keep paired", null).setPositiveButton("Disconnect", (d, i) -> link.disconnect()).show();
        } else if (link.endpoint == null) scan();
        else new AlertDialog.Builder(this).setTitle("Pair with your PC").setItems(
                new String[]{"Reconnect to " + link.endpoint.host, "Scan a new code", "Enter pairing link"},
                (d, i) -> { if (i == 0) link.connect(link.endpoint); else if (i == 1) scan(); else enterLink(); }).show();
    }

    private void scan() { startActivityForResult(new Intent(this, ScanActivity.class), SCAN); }

    private void enterLink() {
        EditText entry = new EditText(this);
        entry.setSingleLine();
        entry.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        entry.setHint("spenmirror://…");
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("Enter pairing link").setView(entry)
                .setMessage("Use Copy pairing link in the PC window.").setPositiveButton("Pair", null)
                .setNegativeButton("Cancel", null).create();
        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            try { link.connect(Endpoint.parse(entry.getText().toString())); dialog.dismiss(); }
            catch (IllegalArgumentException e) { entry.setError(e.getMessage()); }
        }));
        dialog.show();
    }

    private void startMirror() {
        if (link.screen || capturePrompt) return;
        if (app.mirrorBusy()) {
            Toast.makeText(this, "Screen sharing is finishing. Try again in a moment.", Toast.LENGTH_SHORT).show();
            return;
        }
        if (link.writing) {
            pad.cancelStroke();
            link.setWriting(false);
        }
        if (link.state != Link.State.ONLINE) {
            Toast.makeText(this, "Pair with the PC to share your screen.", Toast.LENGTH_SHORT).show();
            if (link.state == Link.State.OFFLINE) pair();
            return;
        }
        capturePrompt = true;
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED && !getPreferences(0).getBoolean("askedNotice", false)) {
            getPreferences(0).edit().putBoolean("askedNotice", true).apply();
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATIONS);
        } else askForCapture();
    }

    private void askForCapture() {
        if (link.state != Link.State.ONLINE) { capturePrompt = false; return; }
        MediaProjectionManager mgr = getSystemService(MediaProjectionManager.class);
        Intent intent = Build.VERSION.SDK_INT >= 34
                ? mgr.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
                : mgr.createScreenCaptureIntent();
        try { startActivityForResult(intent, CAPTURE); }
        catch (RuntimeException e) {
            capturePrompt = false;
            Toast.makeText(this, "Could not open Android’s screen sharing prompt.", Toast.LENGTH_LONG).show();
        }
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request == NOTIFICATIONS) askForCapture();
    }

    private void showInk() {
        if (link.screen) stopService(new Intent(this, MirrorService.class));
        else if (link.writing) {
            pad.cancelStroke();
            link.setWriting(false);
        }
    }

    private void toggleWriting() {
        if (link.writing) {
            pad.cancelStroke();
            link.setWriting(false);
        } else if (link.state != Link.State.ONLINE || link.screen) {
            Toast.makeText(this, "Pair with the PC before writing on it.", Toast.LENGTH_SHORT).show();
            if (link.state == Link.State.OFFLINE) pair();
        } else {
            pad.cancelStroke();
            link.setWriting(true);
        }
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == SCAN) {
            if (result == ScanActivity.ENTER_LINK) enterLink();
            else if (result == RESULT_OK && data != null) {
                try { link.connect(Endpoint.parse(data.getStringExtra("link"))); }
                catch (IllegalArgumentException e) { Toast.makeText(this, e.getMessage(), Toast.LENGTH_LONG).show(); }
            }
        } else if (request == CAPTURE) {
            capturePrompt = false;
            if (result == RESULT_OK && data != null && link.state == Link.State.ONLINE && app.reserveMirror()) {
                try { startForegroundService(new Intent(this, MirrorService.class).putExtra("grant", data)); }
                catch (RuntimeException e) {
                    app.releaseMirror();
                    Toast.makeText(this, "Reopen S-Pen Mirror and try sharing again.", Toast.LENGTH_LONG).show();
                }
            }
        }
    }
}
