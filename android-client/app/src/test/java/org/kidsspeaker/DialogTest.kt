package org.kidsspeaker

import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.LinkedBlockingQueue

/** Разговор с подделками: шлюз — MockWebServer, микрофон — очередь кадров, плеер и звук — записи вызовов. */
class DialogTest {
    private lateinit var server: MockWebServer
    private val replies = LinkedBlockingQueue<JSONObject>()
    private val asks = mutableListOf<RecordedRequest>()

    class FakeMic(private val frames: ArrayDeque<ShortArray>) : FrameSource {
        override fun next(timeoutMs: Long) = frames.removeFirstOrNull()
        override fun drain() {}
    }

    class FakePlayer : TalePlayer {
        val calls = mutableListOf<String>()
        override var item: Pair<String, String>? = null
        override var timerActive = false
        var isLoaded = false
        var isPlaying = false
        override fun loaded() = isLoaded
        override fun playing() = isPlaying
        override fun position() = if (isLoaded) 42.0 else null
        override fun play(url: String, stopAfterSec: Int?, item: Pair<String, String>?, start: Double?) {
            calls += "play $url $stopAfterSec $start"; this.item = item; isLoaded = true; isPlaying = true
            timerActive = stopAfterSec != null
        }
        override fun pause() { calls += "pause"; isPlaying = false }
        override fun resume() { calls += "resume"; if (isLoaded) isPlaying = true }
        override fun stop() { calls += "stop"; isLoaded = false; isPlaying = false; item = null }
        override fun seek(seconds: Int, absolute: Boolean) { calls += "seek $seconds $absolute" }
        override fun setTimer(sec: Int?) { calls += "timer $sec"; timerActive = sec != null }
        override fun tick() {}
    }

    class FakeSounds : Sounds {
        val played = mutableListOf<String>()
        override fun tone(name: String) { played += name }
        override fun playWav(bytes: ByteArray) { played += "wav" }
    }

    class FakeVolume : VolumeControl {
        override var level = 50
        var max = 100
        override fun set(level: Int) { this.level = level.coerceIn(10, max) }
        override fun limit(maxLevel: Int) { max = maxLevel; if (level > max) level = max }
    }

    private val speech = ShortArray(FRAME) { 1000 }
    private val silence = ShortArray(FRAME)
    private val vad = SpeechDetector { f -> f.any { it != 0.toShort() } }

    /** Фраза: 5 кадров речи, потом тишина до конца фразы. */
    private fun phrase() = List(5) { speech } + List(15) { silence }

    @Before
    fun setUp() {
        server = MockWebServer()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path == "/ask" -> { asks += request; MockResponse().setBody((replies.poll() ?: JSONObject()).toString()) }
                request.path!!.startsWith("/speech/") -> MockResponse().setBody(okio.Buffer().write(Wav.encode(ShortArray(10))))
                request.path!!.startsWith("/policy") -> MockResponse().setBody("""{"mode":"night","allow_playback":false,"max_volume":30}""")
                else -> MockResponse().setBody("{}")
            }
        }
        server.start()
    }

    @After
    fun tearDown() = server.shutdown()

    private fun settings() = Settings(server = server.url("/").toString(), user = "speaker", password = "pw",
        deviceId = "kids-room", echoGuardMs = 0)

    private fun reply(heard: String, text: String, action: String? = null, extra: String = "") = JSONObject(
        """{"heard":"$heard","reply":"$text","speech_url":"${server.url("/speech/a.wav")}",
           "action":${action ?: "null"},"limits":{"mode":"normal","allow_playback":true,"max_volume":100,
           "stop_after_sec":null}$extra}""")

    private fun dialog(frames: List<ShortArray>, player: FakePlayer = FakePlayer(), sounds: FakeSounds = FakeSounds(),
                       volume: FakeVolume = FakeVolume()): Triple<Dialog, FakePlayer, FakeSounds> {
        val s = settings()
        val mic = FakeMic(ArrayDeque(frames))
        val d = Dialog(s, Api(s), mic, UtteranceRecorder(mic, vad, s), player, sounds, volume, sleep = {})
        return Triple(d, player, sounds)
    }

    @Test
    fun playCommandStartsTaleWithUrlAndAuth() {
        replies += reply("включи колобка", "Включаю", """{"type":"play","source":"books","item_id":"k1",
            "url":"https://x/m/books/stream/k1","start":120}""")
        val (d, p, snd) = dialog(phrase())
        d.run()
        assertEquals(listOf("play https://x/m/books/stream/k1 null 120.0"), p.calls)
        assertEquals(listOf("wake", "done", "wav"), snd.played)
        val req = asks.single()
        assertTrue(req.getHeader("Authorization")!!.startsWith("Basic "))
        assertTrue(req.body.readUtf8().contains("kids-room"))
    }

    @Test
    fun conversationContinuesWithoutWakeWord() {
        replies += reply("почему небо голубое", "Потому что…")
        replies += reply("а ночью", "Ночью темно")
        val (d, _, snd) = dialog(phrase() + phrase())
        d.run()
        assertEquals(2, asks.size)
        assertTrue("listen" in snd.played)
    }

    @Test
    fun expectReplyKeepsListening() {
        replies += reply("давай познакомимся", "Как тебя зовут?", extra = ""","expect_reply":true""")
        replies += reply("маша", "Маша, правильно?", extra = ""","expect_reply":true""")
        replies += reply("да", "Готово")
        val s = settings().copy(followUpSec = 0f)        // без обычного продолжения — только expect_reply
        val mic = FakeMic(ArrayDeque(phrase() + phrase() + phrase()))
        Dialog(s, Api(s), mic, UtteranceRecorder(mic, vad, s), FakePlayer(), FakeSounds(), FakeVolume(), sleep = {}).run()
        assertEquals(3, asks.size)
    }

    @Test
    fun pausedTaleResumesWhenServerFails() {
        server.shutdown()
        val p = FakePlayer().apply { isLoaded = true; isPlaying = true; item = "books" to "k1" }
        val (d, _, snd) = dialog(phrase(), p)
        d.run()
        assertEquals(listOf("pause", "resume"), p.calls)
        assertTrue("error" in snd.played)
    }

    @Test
    fun silenceAfterWakeResumesTale() {
        val p = FakePlayer().apply { isLoaded = true; isPlaying = true }
        val (d, _, _) = dialog(List(60) { silence }, p)
        assertNull(d.onWake())
        assertEquals(listOf("pause", "resume"), p.calls)
        assertTrue(asks.isEmpty())
    }

    @Test
    fun askSendsWhatIsPlaying() {
        replies += reply("продолжай", "", """{"type":"resume"}""")
        val p = FakePlayer().apply { isLoaded = true; isPlaying = true; item = "books" to "k1" }
        dialog(phrase(), p).first.run()
        val body = asks.single().body.readUtf8()
        assertTrue(body.contains("k1") && body.contains("books") && body.contains("42.0"))
        assertEquals(listOf("pause", "resume"), p.calls)
    }

    @Test
    fun limitsStopAndClampVolume() {
        val p = FakePlayer().apply { isLoaded = true; isPlaying = true }
        val v = FakeVolume().apply { level = 80 }
        val (d, _, _) = dialog(emptyList(), p, volume = v)
        d.applyLimits(Api(settings()).policy())
        assertEquals(listOf("stop"), p.calls)
        assertEquals(30, v.level)
        assertTrue(server.takeRequest().path!!.endsWith("/policy?device_id=kids-room"))
    }

    @Test
    fun volumeCommandResumesTale() {
        replies += reply("потише", "", """{"type":"volume","change":-10}""")
        val p = FakePlayer().apply { isLoaded = true; isPlaying = true }
        val v = FakeVolume()
        dialog(phrase(), p, volume = v).first.run()
        assertEquals(40, v.level)
        assertEquals(listOf("pause", "resume"), p.calls)
        assertFalse(p.calls.contains("stop"))
    }

    @Test
    fun recorderStopsAtSilenceAndGivesUpWithoutSpeech() {
        val s = settings()
        val rec = UtteranceRecorder(FakeMic(ArrayDeque(phrase())), vad, s)
        assertEquals(5 + 12, rec.record()!!.size / FRAME)     // 900 мс тишины = 12 кадров по 80 мс
        var t = 0L
        val quiet = UtteranceRecorder(FakeMic(ArrayDeque(List(100) { silence })), vad, s, clock = { t += 80; t })
        assertNull(quiet.record(1f))
    }

    @Test
    fun wavRoundTrip() {
        val x = ShortArray(100) { (it * 300).toShort() }
        val (y, rate, ch) = Wav.decode(Wav.encode(x))
        assertEquals(16000, rate); assertEquals(1, ch)
        assertTrue(x.contentEquals(y))
    }
}
