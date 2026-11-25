package ink.relay;

import android.app.Application;

public final class RelayApp extends Application {
    final Link link = new Link();
    InkPage page;
    private int mainActivities;
    private boolean mirrorBusy;

    void attachMain() { mainActivities++; }

    void detachMain(boolean recreating) {
        mainActivities--;
        link.main.postDelayed(this::disconnectIfUnused, recreating ? 1000 : 0);
    }

    boolean mirrorBusy() { return mirrorBusy; }

    boolean reserveMirror() {
        if (mirrorBusy) return false;
        mirrorBusy = true;
        return true;
    }

    void releaseMirror() {
        mirrorBusy = false;
        disconnectIfUnused();
    }

    private void disconnectIfUnused() {
        if (mainActivities == 0 && !mirrorBusy && link.state != Link.State.OFFLINE) link.disconnect();
    }

    InkPage page(int width, int height) {
        if (page == null) page = new InkPage(this, width, height);
        return page;
    }
}
