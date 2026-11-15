package org.kidsspeaker

import android.content.Context
import android.media.AudioManager
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.math.roundToInt

/** Плеер сказок — то же, что Player в pi-client (mpv). */
interface TalePlayer {
    /** (модуль, id) загруженного — шлюз проверяет правила при «продолжай». */
    val item: Pair<String, String>?
    val timerActive: Boolean
    fun loaded(): Boolean
    fun playing(): Boolean
    fun position(): Double?
    fun play(url: String, stopAfterSec: Int?, item: Pair<String, String>?, start: Double?)
    fun pause()
    fun resume()
    fun stop()
    fun seek(seconds: Int, absolute: Boolean)
    fun setTimer(sec: Int?)
    /** Часто: прогресс на сервер, автостоп с затуханием. */
    fun tick()
}

/** Громкость 10..100 (как Volume в pi-client). */
interface VolumeControl {
    val level: Int
    fun set(level: Int)
    fun change(delta: Int) = set(level + delta)
    fun limit(maxLevel: Int)
}

class AndroidVolume(ctx: Context, default: Int) : VolumeControl {
    private val am = ctx.getSystemService(AudioManager::class.java)
    private val prefs = ctx.getSharedPreferences("state", Context.MODE_PRIVATE)
    private var maxLevel = 100
    override var level: Int = prefs.getInt("volume", default); private set

    init { apply() }

    override fun set(level: Int) {
        this.level = (Math.round(level / 10.0) * 10).toInt().coerceIn(10, maxLevel)
        Log.i("Volume", "громкость ${this.level} (максимум $maxLevel)")
        apply()
    }

    override fun limit(maxLevel: Int) {
        this.maxLevel = maxLevel.coerceIn(10, 100)
        if (level > this.maxLevel) set(this.maxLevel)
    }

    private fun apply() {
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        am.setStreamVolume(AudioManager.STREAM_MUSIC, (max * level / 100.0).roundToInt().coerceIn(1, max), 0)
        prefs.edit().putInt("volume", level).apply()
    }
}

/**
 * ExoPlayer на своём потоке (все вызовы — через его Looper). Стримы закрыты basic-auth колонки,
 * поэтому заголовки — как у Api. onProgress — позиция на сервер (для «продолжи с того места»).
 */
class ExoTalePlayer(
    ctx: Context,
    headers: Map<String, String>,
    private val fadeSec: Float,
    private val onProgress: (Pair<String, String>, Double, Boolean) -> Unit,
) : TalePlayer {
    private val thread = HandlerThread("player").apply { start() }
    private val handler = Handler(thread.looper)
    private lateinit var exo: ExoPlayer

    @Volatile override var item: Pair<String, String>? = null; private set
    @Volatile private var stopAt: Long? = null
    private var lastReport = 0L
    private var lastPos: Double? = null
    private var duration: Double? = null

    override val timerActive get() = stopAt != null

    init {
        onLooper {
            val http = DefaultHttpDataSource.Factory().setDefaultRequestProperties(headers)
                .setAllowCrossProtocolRedirects(true)
            exo = ExoPlayer.Builder(ctx).setLooper(thread.looper)
                .setMediaSourceFactory(DefaultMediaSourceFactory(http))
                .setAudioAttributes(AudioAttributes.Builder().setUsage(C.USAGE_MEDIA)
                    .setContentType(C.AUDIO_CONTENT_TYPE_SPEECH).build(), false)
                .build()
            exo.addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(state: Int) {
                    if (state == Player.STATE_ENDED) {
                        item?.let { onProgress(it, duration ?: lastPos ?: 0.0, true) }
                        item = null
                        stopAt = null
                    }
                }
            })
        }
    }

    private fun <T> onLooper(block: () -> T): T {
        if (Thread.currentThread() == thread) return block()
        var result: Result<T>? = null
        val done = CountDownLatch(1)
        handler.post { result = runCatching(block); done.countDown() }
        done.await(5, TimeUnit.SECONDS)
        return result?.getOrThrow() ?: error("плеер не ответил")
    }

    override fun loaded() = onLooper { exo.mediaItemCount > 0 && exo.playbackState != Player.STATE_IDLE &&
        exo.playbackState != Player.STATE_ENDED }

    override fun playing() = onLooper { loaded() && exo.playWhenReady }

    override fun position(): Double? = onLooper { if (loaded()) exo.currentPosition / 1000.0 else null }

    override fun play(url: String, stopAfterSec: Int?, item: Pair<String, String>?, start: Double?) {
        report()
        onLooper {
            exo.volume = 1f
            exo.setMediaItem(MediaItem.fromUri(url), ((start ?: 0.0) * 1000).toLong())
            exo.prepare()
            exo.playWhenReady = true
        }
        this.item = item
        lastPos = start
        duration = null
        setTimer(stopAfterSec)
        lastReport = SystemClock.elapsedRealtime()
    }

    override fun pause() = onLooper { exo.playWhenReady = false }

    override fun resume() = onLooper { if (loaded()) exo.playWhenReady = true }

    override fun stop() {
        report()
        onLooper { exo.stop(); exo.clearMediaItems(); exo.volume = 1f }
        stopAt = null
        item = null
    }

    override fun seek(seconds: Int, absolute: Boolean) = onLooper {
        if (loaded()) exo.seekTo(if (absolute) seconds * 1000L else maxOf(0, exo.currentPosition + seconds * 1000L))
    }

    override fun setTimer(sec: Int?) {
        stopAt = sec?.let { SystemClock.elapsedRealtime() + it * 1000L }
        if (sec != null) Log.i("Player", "таймер сна: ${sec / 60} мин")
    }

    private fun report() {
        val it = item ?: return
        val pos = position() ?: lastPos ?: return
        onProgress(it, pos, false)
        lastReport = SystemClock.elapsedRealtime()
    }

    override fun tick() {
        if (item != null) {
            onLooper {
                if (loaded()) {
                    lastPos = exo.currentPosition / 1000.0
                    if (exo.duration != C.TIME_UNSET) duration = exo.duration / 1000.0
                }
            }
            if (playing() && SystemClock.elapsedRealtime() - lastReport > 30_000) report()
        }
        val at = stopAt ?: return
        val left = (at - SystemClock.elapsedRealtime()) / 1000f
        if (left <= 0) {
            Log.i("Player", "таймер сна: стоп")
            stop()
        } else if (left < fadeSec) {
            onLooper { exo.volume = (left / fadeSec).coerceIn(0f, 1f) }
        }
    }

    fun release() = onLooper { exo.release() }.also { thread.quitSafely() }
}
