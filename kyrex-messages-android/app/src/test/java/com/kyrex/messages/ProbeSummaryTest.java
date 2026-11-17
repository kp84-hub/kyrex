package com.kyrex.messages;

import static org.junit.Assert.*;
import org.junit.Test;

public class ProbeSummaryTest {
    @Test public void exactMatchDoesNotTreatEmptyInputAsRcsProof() {
        ProbeSummary summary = new ProbeSummary();
        summary.add(10, "hello", "");
        assertEquals(0, summary.matches);
        assertTrue(summary.matchReport("").contains("not tested"));
    }
    @Test public void matchesLiteralTextAndTracksWindow() {
        ProbeSummary summary = new ProbeSummary();
        summary.add(30, "test a%_b", "a%_b");
        summary.add(10, "test another", "a%_b");
        summary.add(20, null, "a%_b");
        assertEquals(3, summary.count);
        assertEquals(1, summary.matches);
        assertEquals(10, summary.oldest);
        assertEquals(30, summary.newest);
        assertTrue(summary.matchReport("a%_b").contains("does not prove all RCS"));
    }
    @Test public void missingMatchDoesNotAssertNoRcs() {
        assertTrue(new ProbeSummary().matchReport("known text").contains("does not prove"));
    }
    @Test public void recipientCannotInjectMultipleDestinationsOrUriParameters() {
        assertTrue(ProbeSummary.validRecipient("+1 (919) 555-1234"));
        assertFalse(ProbeSummary.validRecipient("9195551234,9195559999"));
        assertFalse(ProbeSummary.validRecipient("9195551234?body=oops"));
        assertFalse(ProbeSummary.validRecipient(""));
    }
}
