package com.kyrex.messages;

/** Counts only. Message bodies and addresses are never retained in a result. */
final class ProbeSummary {
    static final int LIMIT = 5000;
    int count;
    int matches;
    long newest;
    long oldest;
    boolean truncated;

    void add(long date, String body, String phrase) {
        count++;
        newest = Math.max(newest, date);
        oldest = oldest == 0 ? date : Math.min(oldest, date);
        if (!phrase.isEmpty() && body != null && body.contains(phrase)) matches++;
    }

    String matchReport(String phrase) {
        if (phrase.isEmpty()) return "RCS history: not tested. Enter text from a known RCS message first.";
        if (matches > 0) return "Found the exact text in " + matches + " readable records. If you confirmed this message was RCS in Google Messages, that is evidence this phone exposes that message. This does not prove all RCS history is readable.";
        return "Exact text not found. Check that it is within the last 7 days and entered exactly. A missing match does not prove the phone has no RCS messages.";
    }

    static boolean validRecipient(String value) {
        return value.matches("\\+?[0-9][0-9 ()-]{2,30}");
    }
}
