package ink.relay;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

final class Ui {
    static final int INK = 0xff203b2c, GREEN = 0xff295e44, SHELL = 0xfff3f5ef, MUTED = 0xff52665b;
    static int dp(Context c, float value) {
        return Math.round(value * c.getResources().getDisplayMetrics().density);
    }
    static GradientDrawable shape(Context c, int color, int radius) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(color);
        shape.setCornerRadius(dp(c, radius));
        return shape;
    }
    static TextView text(Context c, String label, int size, int color, boolean bold) {
        TextView view = new TextView(c);
        view.setText(label);
        view.setTextSize(size);
        view.setTextColor(color);
        if (bold) view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        return view;
    }
    static Button button(Context c, String label, Runnable action) {
        Button b = new Button(c);
        b.setText(label);
        b.setTextSize(14);
        b.setAllCaps(false);
        b.setSingleLine(true);
        b.setMinWidth(0);
        b.setMinimumWidth(0);
        b.setMinHeight(0);
        b.setMinimumHeight(0);
        b.setPadding(dp(c, 7), 0, dp(c, 7), 0);
        tint(b, false);
        b.setOnClickListener(v -> action.run());
        return b;
    }
    static void tint(Button b, boolean selected) {
        b.setTextColor(selected ? 0xffffffff : INK);
        b.setBackground(new RippleDrawable(ColorStateList.valueOf(0x40729878),
                shape(b.getContext(), selected ? GREEN : 0xffe4eadf, 9), null));
    }
    static LinearLayout row(Context c) {
        LinearLayout row = new LinearLayout(c);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        return row;
    }
    static void item(LinearLayout row, Button b, float weight, int height) {
        Context c = row.getContext();
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                weight == 0 ? -2 : 0, dp(c, height), weight);
        p.setMargins(dp(c, 2), dp(c, 2), dp(c, 2), dp(c, 2));
        row.addView(b, p);
    }
    static void pad(android.view.View view, int x, int y) {
        int px = dp(view.getContext(), x), py = dp(view.getContext(), y);
        view.setPadding(px, py, px, py);
    }
    private Ui() { }
}
