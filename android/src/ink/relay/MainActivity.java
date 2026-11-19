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
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;
import java.util.Locale;

public final class MainActivity extends Activity {
    private static final int SCAN = 10, CAPTURE = 11, NOTIFICATIONS = 12;
    private static final String[] TOOLS = {"Ink pen", "Ballpoint", "Highlighter", "Eraser"};
    private static final String[] COLORS = {"Forest ink", "Blue", "Green", "Coral", "Purple", "Gold", "Black", "White"};
    private Link link;
    private PadView pad;
    private LinearLayout dock, mirrorPanel;
    private TextView status;
    private Button connect, inkMode, screenMode, tool, color, size, undo, redo;
    private boolean capturePrompt;
    private final Runnable linkChanged = this::refreshLink;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        link = ((RelayApp) getApplication()).link;
        capturePrompt = state != null && state.getBoolean("capturePrompt");
        getWindow().setStatusBarColor(Ui.SHELL);
        getWindow().setNavigationBarColor(Ui.SHELL);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
                | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        build();
        link.listen(linkChanged);
        refreshLink();
    }

    private void build() {
        boolean wide = getResources().getConfiguration().orientation == Configuration.ORIENTATION_LANDSCAPE;
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Ui.SHELL);
        LinearLayout header = Ui.row(this);
        Ui.pad(header, 10, wide ? 2 : 5);
        LinearLayout title = new LinearLayout(this);
        title.setOrientation(LinearLayout.VERTICAL);
        title.addView(Ui.text(this, "S-Pen Mirror", wide ? 18 : 21, Ui.INK, true));
        status = Ui.text(this, "", 12, Ui.MUTED, false);
        title.addView(status);
        header.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        LinearLayout modes = Ui.row(this);
        inkMode = Ui.button(this, "Ink canvas", this::showInk);
        screenMode = Ui.button(this, "Screen mirror", this::startMirror);
        Ui.item(modes, inkMode, wide ? 0 : 1, wide ? 42 : 44);
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
        pad = new PadView(this, this::refreshTools);
        area.addView(pad);
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
        root.addView(area, new LinearLayout.LayoutParams(-1, 0, 1));
        dock = new LinearLayout(this);
        dock.setOrientation(LinearLayout.VERTICAL);
        Ui.pad(dock, 6, 3);
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
        Ui.item(edits, Ui.button(this, "Paper", this::choosePaper), 1, 44);
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
        if (pad.page != null) {
            pad.cancelStroke();
            pad.page.save();
        }
        super.onPause();
    }
    @Override protected void onDestroy() {
        link.unlisten(linkChanged);
        if (isFinishing() && !link.screen) link.disconnect();
        super.onDestroy();
    }
    @Override protected void onSaveInstanceState(Bundle out) {
        out.putBoolean("capturePrompt", capturePrompt);
        super.onSaveInstanceState(out);
    }

    private void refreshLink() {
        boolean screen = link.screen;
        status.setText(link.status);
        connect.setText(link.state == Link.State.ONLINE ? "Paired" : link.state == Link.State.CONNECTING ? "Cancel" : "Pair PC");
        Ui.tint(inkMode, !screen);
        Ui.tint(screenMode, screen);
        mirrorPanel.setVisibility(screen ? View.VISIBLE : View.GONE);
        pad.setVisibility(screen ? View.GONE : View.VISIBLE);
        dock.setVisibility(screen ? View.GONE : View.VISIBLE);
        if (!screen) pad.syncPage();
        refreshTools();
    }

    private void refreshTools() {
        InkPage p = pad.page;
        if (p == null) return;
        tool.setText(TOOLS[p.tool.ordinal()] + " ▾");
        size.setText(p.size + " px ▾");
        GradientDrawable swatch = Ui.shape(this, p.color, 8);
        int d = Ui.dp(this, 16);
        swatch.setBounds(0, 0, d, d);
        swatch.setStroke(Ui.dp(this, 1), 0xffa5b2a6);
        color.setCompoundDrawables(swatch, null, null, null);
        color.setCompoundDrawablePadding(Ui.dp(this, 2));
        color.setContentDescription(String.format(Locale.ROOT, "Pen color #%06X", 0xffffff & p.color));
        undo.setEnabled(!p.undo.isEmpty());
        redo.setEnabled(!p.redo.isEmpty());
        undo.setAlpha(undo.isEnabled() ? 1f : 0.45f);
        redo.setAlpha(redo.isEnabled() ? 1f : 0.45f);
    }

    private void chooseTool() {
        InkPage p = pad.page;
        if (p == null) return;
        new AlertDialog.Builder(this).setTitle("Pen tool").setSingleChoiceItems(TOOLS, p.tool.ordinal(), (dialog, i) -> {
            p.tool = InkPage.Tool.values()[i];
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
        TextView label = Ui.text(this, p.size + " px", 20, Ui.INK, true);
        body.addView(label);
        SeekBar slider = new SeekBar(this);
        slider.setMax(35);
        slider.setProgress(p.size - 1);
        slider.setContentDescription("Stroke width");
        slider.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            public void onProgressChanged(SeekBar bar, int value, boolean user) {
                p.size = value + 1; label.setText(p.size + " px"); refreshTools();
            }
            public void onStartTrackingTouch(SeekBar bar) { }
            public void onStopTrackingTouch(SeekBar bar) { p.saveSettings(); }
        });
        body.addView(slider, new LinearLayout.LayoutParams(-1, Ui.dp(this, 52)));
        new AlertDialog.Builder(this).setTitle("Stroke width").setView(body).setPositiveButton("Done", null)
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
        pad.cancelStroke(); pad.page.step(forward); pad.syncPage(); refreshTools();
    }

    private void more() {
        InkPage p = pad.page;
        if (p == null) return;
        String[] options = {p.fingers ? "Use S-Pen only" : "Allow finger drawing", "Clear canvas"};
        new AlertDialog.Builder(this).setItems(options, (d, i) -> {
            if (i == 0) { p.fingers = !p.fingers; p.saveSettings(); }
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
        startActivityForResult(intent, CAPTURE);
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request == NOTIFICATIONS) askForCapture();
    }

    private void showInk() {
        if (link.screen) stopService(new Intent(this, MirrorService.class));
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
            if (result == RESULT_OK && data != null && link.state == Link.State.ONLINE) {
                try { startForegroundService(new Intent(this, MirrorService.class).putExtra("grant", data)); }
                catch (RuntimeException e) { Toast.makeText(this, "Reopen S-Pen Mirror and try sharing again.", Toast.LENGTH_LONG).show(); }
            }
        }
    }
}
