package org.kidsspeaker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log

/**
 * Колонка: постоянно слушает слово активации, ведёт разговор, играет сказки, раз в минуту
 * спрашивает у шлюза лимиты. Работает foreground-службой (уведомление «Колонка слушает»).
 */
class SpeakerService : Service() {
    companion object {
        const val ACTION_TALK = "org.kidsspeaker.TALK"
        const val ACTION_STOP_TALE = "org.kidsspeaker.STOP_TALE"
        private const val CHANNEL = "speaker"
        private const val TAG = "Speaker"

        /** Экран подписывается на статус. */
        @Volatile var listener: ((Status, String?) -> Unit)? = null
        @Volatile var status: Pair<Status, String?> = Status.IDLE to null; private set
        @Volatile var running = false; private set

        fun start(ctx: Context, action: String? = null) {
            ctx.startForegroundService(Intent(ctx, SpeakerService::class.java).setAction(action))
        }

        fun restart(ctx: Context) {
            ctx.stopService(Intent(ctx, SpeakerService::class.java))
            start(ctx)
        }
    }

    private lateinit var s: Settings
    private var mic: Mic? = null
    private var wake: WakeWord? = null
    private var player: ExoTalePlayer? = null
    private var dialog: Dialog? = null
    private var api: Api? = null
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var talkRequested = false
    @Volatile private var busy = false
    @Volatile private var stopping = false

    private fun setStatus(st: Status, text: String?) {
        status = st to text
        listener?.invoke(st, text)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        running = true
        startForeground(1, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "kidsspeaker:listen").apply { acquire() }
        s = Settings.load(this)
        if (!s.configured) {
            setStatus(Status.ERROR, "Задайте пароль колонки в настройках")
            return
        }
        try {
            val a = Api(s).also { api = it }
            val p = ExoTalePlayer(this, a.headers, s.fadeSec) { item, pos, finished ->
                Thread { runCatching { a.progress(item, pos, finished) }.onFailure { Log.w(TAG, "позиция: $it") } }.start()
            }.also { player = it }
            val m = Mic().also { mic = it }
            wake = WakeWord(asset("melspectrogram.onnx"), asset("embedding_model.onnx"), asset("hey_jarvis_v0.1.onnx"))
            dialog = Dialog(s, a, m, UtteranceRecorder(m, webRtcDetector(2), s), p, AndroidSounds(),
                AndroidVolume(this, s.defaultVolume), ::setStatus)
            m.start()
            Thread(::listenLoop, "listen").apply { isDaemon = true; start() }
            Thread(::background, "background").apply { isDaemon = true; start() }
            setStatus(Status.IDLE, null)
        } catch (e: Exception) {
            Log.e(TAG, "не запустилась", e)
            setStatus(Status.ERROR, "Ошибка запуска: ${e.message}")
        }
    }

    private fun asset(name: String) = assets.open(name).use { it.readBytes() }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_TALK -> talkRequested = true
            ACTION_STOP_TALE -> Thread { runCatching { player?.stop() } }.start()
        }
        return START_STICKY
    }

    /** Главный цикл: кадры микрофона → слово активации (или кнопка) → разговор. */
    private fun listenLoop() {
        val m = mic ?: return
        val w = wake ?: return
        Log.i(TAG, "слушаю (порог ${s.wakeThreshold})")
        while (!stopping) {
            val frame = m.next(500)
            var start = talkRequested
            if (frame != null && !start) {
                val score = try { w.process(frame) } catch (e: Exception) { Log.e(TAG, "wake", e); 0f }
                if (w.warmedUp && score >= s.wakeThreshold) {
                    Log.i(TAG, "слово активации (%.2f)".format(score))
                    start = true
                }
            }
            if (!start) continue
            talkRequested = false
            busy = true
            try {
                dialog?.run()
            } catch (e: Exception) {
                Log.e(TAG, "ошибка в диалоге", e)
                setStatus(Status.IDLE, null)
            } finally {
                busy = false
                w.reset()
                m.drain()
            }
        }
    }

    /** Таймер сна и лимиты. Ни одна ошибка не должна убить этот поток: без него ночью нет автостопа. */
    private fun background() {
        var lastPolicy = 0L
        while (!stopping) {
            try {
                val now = SystemClock.elapsedRealtime()
                if (now - lastPolicy > 60_000 && !busy) {
                    lastPolicy = now
                    try { dialog?.applyLimits(api?.policy()) } catch (e: Exception) { Log.w(TAG, "policy: $e") }
                }
                player?.tick()
                val p = player
                if (!busy && p != null && status.first == Status.IDLE) {
                    val playing = runCatching { p.playing() }.getOrDefault(false)
                    listener?.invoke(Status.IDLE, if (playing) "Играет сказка" else null)
                }
            } catch (e: Exception) {
                Log.e(TAG, "фоновый цикл", e)
            }
            SystemClock.sleep(500)
        }
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Колонка", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_speaker)
            .setContentTitle("Колонка слушает")
            .setContentText("Скажите «Hey Jarvis» или нажмите «Говорить»")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        stopping = true
        running = false
        runCatching { mic?.stop() }
        runCatching { player?.stop(); player?.release() }
        runCatching { wake?.close() }
        runCatching { wakeLock?.release() }
        super.onDestroy()
    }
}
