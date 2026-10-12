package com.kyrex.voice

import android.Manifest
import android.app.Service
import android.content.Context
import android.content.Intent
import android.speech.SpeechRecognizer
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
}
