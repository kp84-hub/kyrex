// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

/** Connection-only recovery. No send operation or confirmation is replayed. */
final class ConnectionRecovery {
    private enum State { CHECK, PROBING, RESTORE, CONNECTING, READY, STOPPED }
    private State state = State.STOPPED;
    private int attempts;

    void manualReconnect() { state = State.RESTORE; attempts = 0; }
    void disconnected() { if (state == State.READY) state = State.CHECK; }
    void verified() { if (state != State.STOPPED) { state = State.READY; attempts = 0; } }
    void stop() { state = State.STOPPED; }
    boolean enabled() { return state != State.STOPPED; }
    boolean ready() { return state == State.READY; }
    String presence() { return ready() ? "ready" : enabled() ? "reconnecting" : "needs_attention"; }
    boolean running() { return state == State.PROBING || state == State.CONNECTING; }

    boolean beginProbe(boolean active, boolean busy) {
        if (!active || busy || state != State.CHECK) return false;
        state = State.PROBING;
        return true;
    }
    void probeFailed() { if (state == State.PROBING) state = State.RESTORE; }
    boolean beginRestore(boolean active, boolean busy) {
        if (!active || busy || state != State.RESTORE) return false;
        state = State.CONNECTING;
        attempts = Math.min(attempts + 1, 5);
        return true;
    }
    void restoreFailed() { if (state == State.CONNECTING) state = State.RESTORE; }
    long retryDelay() { return Math.min(60000, 5000L << Math.max(0, attempts - 1)); }
}
