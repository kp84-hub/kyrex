package com.kyrex.voice

import android.Manifest
import android.app.Service
import android.content.Context
import android.content.Intent
import android.speech.SpeechRecognizer
import android.os.Bundle
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.time.Duration
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28, 35])
class VoiceServiceTest {
    class FakeService : VoiceService() {
        var starts = 0
        var stops = 0
        var wake: (() -> Unit)? = null
        var failWake: ((String) -> Unit)? = null
        var captures = 0
        var delayHandoff = false
        var finishHandoff: ((Boolean) -> Unit)? = null
        var aiSpeech = false
        @Volatile var heartbeatError: Exception? = null
        @Volatile var renewals = 0
        var renewAction: (() -> Unit)? = null
        var synthesis: (String) -> File = { throw IllegalStateException("Speech quota reached.") }
        override fun ask(text: String) = "Hello Keith."
        override fun connectVoice(ready: (String) -> Unit, failure: (String) -> Unit) { ready("test-only-lease") }
        override fun renewListening(secret: String) { renewals++; renewAction?.invoke(); heartbeatError?.let { throw it } }
        override fun releaseListening(secret: String) {}
        override fun sharedVoiceSettings() = org.json.JSONObject().put("engine", if (aiSpeech) "openai" else "device")
        override fun createNaturalVoice() = object : CloudVoice(this@FakeService) {
            override fun render(text: String, lease: String) = synthesis(text)
        }
        override fun linked() = true
        override fun beginSpeechCapture() { captures++ }
        override fun createWakeListener() = object : WakeListener(this) {
            override fun start(wake: () -> Unit, failure: (String) -> Unit, ready: () -> Unit, level: (Int) -> Unit) {
                starts++; this@FakeService.wake = wake; failWake = failure; ready(); level(17)
            }
            override fun stop() { stops++ }
            override fun stopAndWait(done: (Boolean) -> Unit) {
                stop()
                if (delayHandoff) finishHandoff = done else done(true)
            }
        }
    }
    @Test fun explicitStartStopAndStaleWakeDoNotRestartMic() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        assertEquals(Service.START_NOT_STICKY, service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1))
        assertEquals(1, service.starts)
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 2)
        assertEquals(1, service.starts)
        service.onStartCommand(Intent().setAction(VoiceService.STOP), 0, 3)
        service.wake?.invoke()
        shadowOf(android.os.Looper.getMainLooper()).idle()
        assertEquals(1, service.starts)
        assertTrue(service.stops > 0)
        controller.destroy()
    }
    @Test fun systemRestartDoesNotEnableListening() {
        val controller = Robolectric.buildService(FakeService::class.java).create()
        assertEquals(Service.START_NOT_STICKY, controller.get().onStartCommand(null, 0, 1))
        assertEquals(0, controller.get().starts)
        controller.destroy()
    }
    @Test fun missingMicrophonePermissionFailsBeforeWakeCapture() {
        shadowOf(RuntimeEnvironment.getApplication()).denyPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        controller.get().onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        assertEquals(0, controller.get().starts)
        controller.destroy()
    }
    @Test fun talkNowWorksWithoutStartingWakeAndStopsAfterRecognitionError() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.onStartCommand(Intent().setAction(VoiceService.TALK), 0, 1)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        assertEquals(0, service.starts)
        assertEquals(1, service.captures)
        service.onError(SpeechRecognizer.ERROR_NO_MATCH)
        assertFalse(VoiceService.active)
        val prefs = service.getSharedPreferences("voice-status", Context.MODE_PRIVATE)
        assertTrue(prefs.getString("issue", "")!!.contains("No speech was recognized"))
        controller.destroy()
        assertTrue(prefs.getString("issue", "")!!.contains("No speech was recognized"))
    }
    @Test fun talkDuringWakeListeningReleasesRecorderAndReturnsToWake() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        assertEquals(17, VoiceService.microphoneLevel)
        val oldWake = service.wake
        service.onStartCommand(Intent().setAction(VoiceService.TALK), 0, 2)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        assertEquals(1, service.captures)
        assertTrue(service.stops > 0)
        service.onError(SpeechRecognizer.ERROR_NO_MATCH)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        assertEquals(2, service.starts)
        oldWake?.invoke()
        shadowOf(android.os.Looper.getMainLooper()).idle()
        assertEquals(1, service.captures)
        service.onStartCommand(Intent().setAction(VoiceService.STOP), 0, 3)
        controller.destroy()
    }
    @Test fun wakeErrorsSurviveServiceStopAndAreVisibleInTheApp() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        service.failWake?.invoke("Microphone read failed (-6).")
        shadowOf(android.os.Looper.getMainLooper()).idle()
        assertFalse(VoiceService.active)
        controller.destroy()
        assertEquals("Microphone read failed (-6).", service.getSharedPreferences("voice-status", Context.MODE_PRIVATE).getString("issue", ""))
    }
    @Test fun stopDuringMicHandoffNeverStartsSpeechCaptureLater() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        service.delayHandoff = true
        service.onStartCommand(Intent().setAction(VoiceService.TALK), 0, 2)
        assertNotNull(service.finishHandoff)
        service.onStartCommand(Intent().setAction(VoiceService.STOP), 0, 3)
        service.finishHandoff?.invoke(true)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        assertEquals(0, service.captures)
        assertFalse(VoiceService.active)
        controller.destroy()
    }
    private fun awaitMain(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (!condition() && System.nanoTime() < deadline) {
            shadowOf(android.os.Looper.getMainLooper()).idle()
            Thread.sleep(10)
        }
        assertTrue("Worker result did not reach main thread", condition())
    }
    private fun results() = Bundle().apply {
        putStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION, arrayListOf("Say hello"))
    }
    @Test fun temporaryHeartbeatFailurePausesAndResumesSameListeningSession() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        val staleWake = service.wake
        service.heartbeatError = java.net.SocketTimeoutException("Temporary timeout")
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(15))
        val prefs = service.getSharedPreferences("voice-status", Context.MODE_PRIVATE)
        awaitMain { prefs.getString("state", "") == "Reconnecting voice…" }
        assertTrue(VoiceService.active); assertTrue(service.stops > 0)
        assertEquals(0, VoiceService.microphoneLevel)
        staleWake?.invoke(); shadowOf(android.os.Looper.getMainLooper()).idle()
        assertEquals(0, service.captures)
        service.heartbeatError = null
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(15))
        awaitMain { prefs.getString("state", "") == "Listening for Hey Kyrex" && service.starts == 2 }
        assertTrue(VoiceService.active)
        assertEquals("Listening for Hey Kyrex", prefs.getString("state", ""))
        assertEquals("", prefs.getString("issue", ""))
        assertEquals(0, service.captures)
        service.onStartCommand(Intent().setAction(VoiceService.STOP), 0, 2)
        controller.destroy()
    }
    @Test fun confirmedTakeoverStopsAndNeverReacquiresListening() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        service.heartbeatError = VoiceSessionLost("Another session took over listening.")
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(15))
        awaitMain { !VoiceService.active }
        assertEquals(1, service.starts)
        assertEquals("Another session took over listening.", service.getSharedPreferences("voice-status", Context.MODE_PRIVATE).getString("issue", ""))
        service.heartbeatError = null
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(30))
        assertEquals(1, service.renewals)
        assertEquals(1, service.starts)
        controller.destroy()
    }
    @Test fun explicitStopDuringReconnectNeverResumesTheMicrophone() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        service.heartbeatError = java.io.IOException("Offline")
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(15))
        awaitMain { service.renewals == 1 && service.stops > 0 }
        service.onStartCommand(Intent().setAction(VoiceService.STOP), 0, 2)
        service.heartbeatError = null
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(30))
        assertFalse(VoiceService.active); assertEquals(1, service.starts)
        assertEquals(1, service.renewals)
        controller.destroy()
    }
    @Test fun prolongedNetworkLossEndsListeningWithAnAccurateError() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        service.heartbeatError = java.io.IOException("Offline")
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(15))
        val prefs = service.getSharedPreferences("voice-status", Context.MODE_PRIVATE)
        awaitMain { prefs.getString("state", "") == "Reconnecting voice…" }
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(90))
        awaitMain { !VoiceService.active }
        assertTrue(prefs.getString("issue", "")!!.contains("could not recover"))
        assertEquals(1, service.starts)
        controller.destroy()
    }
    @Test fun lateSuccessfulHeartbeatAfterStopCannotRestartWake() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        val entered = CountDownLatch(1); val finish = CountDownLatch(1)
        service.renewAction = { entered.countDown(); check(finish.await(5, TimeUnit.SECONDS)) }
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(15))
        assertTrue(entered.await(5, TimeUnit.SECONDS))
        service.onStartCommand(Intent().setAction(VoiceService.STOP), 0, 2)
        finish.countDown()
        shadowOf(android.os.Looper.getMainLooper()).idleFor(Duration.ofSeconds(30))
        assertFalse(VoiceService.active); assertEquals(1, service.starts)
        controller.destroy()
    }
    @Test fun failedAiSpeechPreservesErrorAndCompletesOneShot() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        service.aiSpeech = true
        service.onStartCommand(Intent().setAction(VoiceService.TALK), 0, 1)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        service.onResults(results())
        awaitMain { !VoiceService.active }
        assertEquals("Speech quota reached.", service.getSharedPreferences("voice-status", Context.MODE_PRIVATE).getString("issue", ""))
        assertEquals(0, service.starts)
        controller.destroy()
    }
    @Test fun stopWhileAiSpeechIsRenderingDeletesLateAudioAndNeverRestartsWake() {
        shadowOf(RuntimeEnvironment.getApplication()).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val controller = Robolectric.buildService(FakeService::class.java).create()
        val service = controller.get()
        val started = CountDownLatch(1)
        val finish = CountDownLatch(1)
        val rendered = CountDownLatch(1)
        val audio = File(service.cacheDir, "kyrex-ai-late.mp3")
        service.aiSpeech = true
        service.synthesis = {
            started.countDown()
            check(finish.await(5, TimeUnit.SECONDS))
            audio.writeText("late fake speech")
            rendered.countDown()
            audio
        }
        service.onStartCommand(Intent().setAction(VoiceService.START), 0, 1)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        service.onStartCommand(Intent().setAction(VoiceService.TALK), 0, 2)
        shadowOf(android.os.Looper.getMainLooper()).idle()
        service.onResults(results())
        awaitMain { started.count == 0L }
        service.onStartCommand(Intent().setAction(VoiceService.STOP), 0, 3)
        finish.countDown()
        assertTrue(rendered.await(5, TimeUnit.SECONDS))
        awaitMain { !audio.exists() }
        assertFalse(VoiceService.active)
        assertEquals(1, service.starts)
        controller.destroy()
    }

}
