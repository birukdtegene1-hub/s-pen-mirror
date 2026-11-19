package ink.relay;

import android.app.Application;

public final class RelayApp extends Application {
    final Link link = new Link();
    InkPage page;

    InkPage page(int width, int height) {
        if (page == null) page = new InkPage(this, width, height);
        return page;
    }
}
