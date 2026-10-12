package com.kyrex.voice

import android.Manifest
import android.app.Service
import android.content.Intent
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
        override fun linked() = true
        override fun createWakeListener() = object : WakeListener(this) {
            override fun start(wake: () -> Unit, failure: () -> Unit) { starts++; this@FakeService.wake = wake }
            override fun stop() { stops++ }
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
}
