package org.kidsspeaker

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.math.PI
import kotlin.math.min
import kotlin.math.sin

const val RATE = 16000
const val FRAME = WakeWord.CHUNK          // 80 мс
private const val SUB = 320               // 20 мс — кадр VAD

// ---------------------------------------------------------------- wav

object Wav {
    fun encode(samples: ShortArray, rate: Int = RATE): ByteArray {
        val data = samples.size * 2
        val b = ByteBuffer.allocate(44 + data).order(ByteOrder.LITTLE_ENDIAN)
        b.put("RIFF".toByteArray()).putInt(36 + data).put("WAVE".toByteArray())
        b.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(rate).putInt(rate * 2)
            .putShort(2).putShort(16)
        b.put("data".toByteArray()).putInt(data)
        samples.forEach { b.putShort(it) }
        return b.array()
    }

    /** PCM 16 бит: (отсчёты, частота, каналов). Ищет чанки fmt и data. */
    fun decode(bytes: ByteArray): Triple<ShortArray, Int, Int> {
        val b = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
        require(bytes.size > 12 && String(bytes, 0, 4) == "RIFF" && String(bytes, 8, 4) == "WAVE") { "не wav" }
        var pos = 12
        var rate = RATE
        var channels = 1
        while (pos + 8 <= bytes.size) {
            val id = String(bytes, pos, 4)
            val size = b.getInt(pos + 4)
            val body = pos + 8
            if (id == "fmt ") {
                channels = b.getShort(body + 2).toInt()
                rate = b.getInt(body + 4)
                require(b.getShort(body + 14).toInt() == 16) { "нужен 16-битный wav" }
            } else if (id == "data") {
                val n = min(size, bytes.size - body) / 2
                val out = ShortArray(n) { b.getShort(body + it * 2) }
                return Triple(out, rate, channels)
            }
            pos = body + size + (size and 1)
        }
        error("в wav нет данных")
    }
}

// ---------------------------------------------------------------- речь или нет

fun interface SpeechDetector {
    /** frame — 20 мс (320 отсчётов). */
    fun isSpeech(frame: ShortArray): Boolean
}

/** WebRTC VAD (как webrtcvad в pi-client). mode 0..3: выше — строже. */
fun webRtcDetector(mode: Int = 2): SpeechDetector {
    val vad = com.konovalov.vad.webrtc.VadWebRTC(
        sampleRate = com.konovalov.vad.webrtc.config.SampleRate.SAMPLE_RATE_16K,
        frameSize = com.konovalov.vad.webrtc.config.FrameSize.FRAME_SIZE_320,
        mode = when (mode) {
            0 -> com.konovalov.vad.webrtc.config.Mode.NORMAL
            1 -> com.konovalov.vad.webrtc.config.Mode.LOW_BITRATE
            2 -> com.konovalov.vad.webrtc.config.Mode.AGGRESSIVE
            else -> com.konovalov.vad.webrtc.config.Mode.VERY_AGGRESSIVE
        },
    )
    return SpeechDetector { vad.isSpeech(it) }
}

// ---------------------------------------------------------------- микрофон

/** Источник кадров по 80 мс. */
interface FrameSource {
    /** Следующий кадр или null, если микрофон молчит дольше timeoutMs. */
    fun next(timeoutMs: Long = 2000): ShortArray?
    fun drain()
}

/** Микрофон телефона: отдельный поток читает AudioRecord кадрами по 1280 отсчётов. */
class Mic : FrameSource {
    private val queue = ArrayBlockingQueue<ShortArray>(200)
    @Volatile private var running = false
    private var thread: Thread? = null

    @SuppressLint("MissingPermission")   // разрешение проверяет MainActivity до запуска службы
    fun start() {
        if (running) return
        running = true
        thread = Thread({
            val min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(min, FRAME * 2 * 4))
            try {
                rec.startRecording()
                while (running) {
                    val buf = ShortArray(FRAME)
                    var got = 0
                    while (got < FRAME && running) {
                        val n = rec.read(buf, got, FRAME - got)
                        if (n < 0) { Log.w("Mic", "read $n"); Thread.sleep(100); break }
                        got += n
                    }
                    if (got == FRAME && !queue.offer(buf)) { queue.poll(); queue.offer(buf) }
                }
            } finally {
                rec.stop()
                rec.release()
            }
        }, "mic").apply { isDaemon = true; start() }
    }

    fun stop() { running = false; thread?.join(1000) }

    override fun next(timeoutMs: Long): ShortArray? = queue.poll(timeoutMs, TimeUnit.MILLISECONDS)
    override fun drain() = queue.clear()
}

/** Запись фразы до тишины — как Client.record_utterance в pi-client. */
class UtteranceRecorder(
    private val src: FrameSource,
    private val vad: SpeechDetector,
    private val s: Settings,
    private val clock: () -> Long = System::currentTimeMillis,
) {
    /** null — никто не заговорил за noSpeechSec. */
    fun record(noSpeechSec: Float = s.noSpeechSec): ShortArray? {
        val frames = ArrayList<ShortArray>()
        var started = false
        var silenceMs = 0
        val t0 = clock()
        while (true) {
            val frame = src.next() ?: return if (started) join(frames) else null
            frames.add(frame)
            var voiced = 0
            for (i in 0 until FRAME step SUB) if (vad.isSpeech(frame.copyOfRange(i, i + SUB))) voiced++
            if (voiced >= 2) { started = true; silenceMs = 0 } else silenceMs += 80
            val elapsed = (clock() - t0) / 1000f
            if (!started && elapsed > noSpeechSec) return null
            if (started && silenceMs >= s.endSilenceMs) break
            if (elapsed > s.maxRecordSec) break
        }
        return join(frames)
    }

    private fun join(frames: List<ShortArray>): ShortArray {
        val out = ShortArray(frames.sumOf { it.size })
        var p = 0
        for (f in frames) { f.copyInto(out, p); p += f.size }
        return out
    }
}

// ---------------------------------------------------------------- звук: сигналы и ответы

interface Sounds {
    fun tone(name: String)
    /** Проиграть wav (озвученный ответ) и дождаться конца. */
    fun playWav(bytes: ByteArray)
}

/** Сигналы как make_tone в pi-client и проигрывание ответов через AudioTrack (блокирующе). */
class AndroidSounds : Sounds {
    private val tones = mapOf(
        "wake" to make(listOf(880.0 to 0.12)),
        "listen" to make(listOf(990.0 to 0.06), 0.12),
        "done" to make(listOf(660.0 to 0.08)),
        "error" to make(listOf(330.0 to 0.15, 220.0 to 0.25)),
    )

    override fun tone(name: String) { tones[name]?.let { play(it, RATE, 1) } }

    override fun playWav(bytes: ByteArray) {
        val (pcm, rate, ch) = Wav.decode(bytes)
        play(pcm, rate, ch)
    }

    private fun play(pcm: ShortArray, rate: Int, channels: Int) {
        if (pcm.isEmpty()) return
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(rate).setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(if (channels == 2) AudioFormat.CHANNEL_OUT_STEREO else AudioFormat.CHANNEL_OUT_MONO)
                .build())
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(maxOf(AudioTrack.getMinBufferSize(rate,
                if (channels == 2) AudioFormat.CHANNEL_OUT_STEREO else AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT), 8192))
            .build()
        try {
            track.play()
            track.write(pcm, 0, pcm.size)                 // блокирует, пока всё не ушло в буфер
            val frames = pcm.size / channels
            val deadline = System.currentTimeMillis() + frames * 1000L / rate + 2000
            while (track.playbackHeadPosition < frames && System.currentTimeMillis() < deadline) Thread.sleep(20)
        } finally {
            track.stop()
            track.release()
        }
    }

    companion object {
        fun make(tones: List<Pair<Double, Double>>, volume: Double = 0.3): ShortArray {
            val out = ArrayList<Short>()
            for ((freq, dur) in tones) {
                val n = (RATE * dur).toInt()
                for (i in 0 until n) {
                    val t = i.toDouble() / RATE
                    val env = minOf(1.0, minOf(t, dur - t) * 40)
                    out.add((sin(2 * PI * freq * t) * env * volume * 32767).toInt().toShort())
                }
            }
            return out.toShortArray()
        }
    }
}
