// SPDX-License-Identifier: AGPL-3.0-or-later
package com.kyrex.pairing;

/** A notification cannot imply a working cloud connection after a failed/stale check-in. */
final class CloudPresence {
    private long acknowledgedAt = -1;
    void acknowledged(long elapsed) { acknowledgedAt = elapsed; }
    void failed() { reset(); }
    void reset() { acknowledgedAt = -1; }
    boolean current(long elapsed) {
        return acknowledgedAt >= 0 && elapsed >= acknowledgedAt && elapsed - acknowledgedAt < 45000;
    }
}
