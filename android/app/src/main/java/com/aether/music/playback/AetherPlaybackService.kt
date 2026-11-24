package com.aether.music.playback

import android.app.PendingIntent
import android.content.Intent
import android.media.audiofx.Equalizer
import android.net.Uri
import android.os.Bundle
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.ResolvingDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.LibraryResult
import androidx.media3.session.MediaLibraryService
import androidx.media3.session.MediaSession
import androidx.media3.session.SessionCommand
import androidx.media3.session.SessionResult
import com.aether.music.AetherApplication
import com.aether.music.MainActivity
import com.aether.music.data.EqPreset
import com.aether.music.data.Track
import com.google.common.collect.ImmutableList
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import java.io.IOException

@OptIn(UnstableApi::class)
class AetherPlaybackService : MediaLibraryService() {

    private var player: ExoPlayer? = null
    private var mediaLibrarySession: MediaLibrarySession? = null
    private var equalizer: Equalizer? = null

    private val app get() = AetherApplication.instance
    private val youtube get() = app.youtubeClient
    private val library get() = app.libraryStore

    override fun onCreate() {
        super.onCreate()

        val okHttpFactory = OkHttpDataSource.Factory(youtube.httpClient)
        val resolvingFactory = ResolvingDataSource.Factory(
            okHttpFactory,
            object : ResolvingDataSource.Resolver {
                override fun resolveDataSpec(dataSpec: DataSpec): DataSpec {
                    val uri = dataSpec.uri
                    if (uri.scheme == Track.SCHEME) {
                        val videoId = uri.host?.takeIf { it.isNotBlank() }
                            ?: uri.lastPathSegment
                            ?: throw IOException("Missing videoId in URI: $uri")
                        val resolved = youtube.resolveAudioStream(videoId, forceRefresh = false)
                            ?: youtube.resolveAudioStream(videoId, forceRefresh = true)
                            ?: throw IOException("Failed to resolve audio stream for $videoId")

                        return dataSpec.buildUpon()
                            .setUri(Uri.parse(resolved.url))
                            .setHttpRequestHeaders(mapOf("User-Agent" to resolved.userAgent))
                            .build()
                    }
                    return dataSpec
                }
            }
        )

        val audioAttributes = AudioAttributes.Builder()
            .setUsage(C.USAGE_MEDIA)
            .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
            .build()

        val exo = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(resolvingFactory))
            .setAudioAttributes(audioAttributes, true)
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()

        exo.addListener(object : Player.Listener {
            override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
                val track = Track.fromMediaItem(mediaItem) ?: return
                library.recordPlay(track)
                persistCurrentQueue(exo)
            }

            override fun onAudioSessionIdChanged(audioSessionId: Int) {
                attachEqualizer(audioSessionId, library.current.eqPreset)
            }

            override fun onPlayerError(error: PlaybackException) {
                // Invalidate cached URL on HTTP 403/410 and retry once or advance
                val curId = exo.currentMediaItem?.mediaId
                if (!curId.isNullOrBlank()) {
                    youtube.invalidateStream(curId)
                }
                if (exo.hasNextMediaItem()) {
                    exo.seekToNextMediaItem()
                    exo.prepare()
                    exo.playWhenReady = true
                }
            }
        })

        player = exo
        attachEqualizer(exo.audioSessionId, library.current.eqPreset)

        val openAppIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val sessionActivityPendingIntent = PendingIntent.getActivity(
            this,
            0,
            openAppIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        mediaLibrarySession = MediaLibrarySession.Builder(this, exo, LibraryCallback())
            .setSessionActivity(sessionActivityPendingIntent)
            .build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaLibrarySession? {
        return mediaLibrarySession
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        val exo = player
        if (exo == null || !exo.playWhenReady || exo.mediaItemCount == 0) {
            stopSelf()
        }
    }

    override fun onDestroy() {
        releaseEqualizer()
        mediaLibrarySession?.run {
            player.release()
            release()
        }
        mediaLibrarySession = null
        player = null
        super.onDestroy()
    }

    private fun persistCurrentQueue(exo: ExoPlayer) {
        val items = mutableListOf<Track>()
        for (i in 0 until exo.mediaItemCount) {
            Track.fromMediaItem(exo.getMediaItemAt(i))?.let { items += it }
        }
        if (items.isNotEmpty()) {
            library.saveLastQueue(items)
        }
    }

    private fun attachEqualizer(audioSessionId: Int, preset: EqPreset) {
        if (audioSessionId == C.AUDIO_SESSION_ID_UNSET || audioSessionId <= 0) return
        releaseEqualizer()
        try {
            val eq = Equalizer(0, audioSessionId)
            eq.enabled = true
            equalizer = eq
            applyEqBands(eq, preset)
        } catch (_: Exception) {
            // Some devices restrict audio effects in certain modes
        }
    }

    private fun applyEqPreset(preset: EqPreset) {
        library.setEqPreset(preset)
        val eq = equalizer
        if (eq != null) {
            try {
                applyEqBands(eq, preset)
            } catch (_: Exception) {
            }
        } else {
            player?.let { attachEqualizer(it.audioSessionId, preset) }
        }
    }

    private fun applyEqBands(eq: Equalizer, preset: EqPreset) {
        val bands = eq.numberOfBands.toInt()
        if (bands <= 0) return
        val range = eq.bandLevelRange
        val minLv = range[0].toInt()
        val maxLv = range[1].toInt()

        for (b in 0 until bands) {
            val freqHz = eq.getCenterFreq(b.toShort()) / 1000
            val gainMb = when (preset) {
                EqPreset.FLAT -> 0
                EqPreset.BASS -> when {
                    freqHz <= 180 -> 450
                    freqHz <= 400 -> 200
                    freqHz >= 6000 -> 120
                    else -> 0
                }
                EqPreset.VOCAL -> when {
                    freqHz <= 250 -> -150
                    freqHz in 1000..4500 -> 350
                    freqHz >= 7000 -> 150
                    else -> 0
                }
                EqPreset.NIGHT -> when {
                    freqHz <= 180 -> 180
                    freqHz >= 5000 -> -250
                    else -> 0
                }
                EqPreset.DRIVE -> when {
                    freqHz <= 180 -> 380
                    freqHz in 800..2000 -> -100
                    freqHz >= 6000 -> 300
                    else -> 0
                }
            }
            val clamped = gainMb.coerceIn(minLv, maxLv).toShort()
            eq.setBandLevel(b.toShort(), clamped)
        }
    }

    private fun releaseEqualizer() {
        try {
            equalizer?.release()
        } catch (_: Exception) {
        }
        equalizer = null
    }

    private inner class LibraryCallback : MediaLibrarySession.Callback {

        override fun onConnect(
            session: MediaSession,
            controller: MediaSession.ControllerInfo,
        ): MediaSession.ConnectionResult {
            val sessionCommands = MediaSession.ConnectionResult.DEFAULT_SESSION_AND_LIBRARY_COMMANDS
                .buildUpon()
                .add(SessionCommand(CMD_SET_EQ, Bundle.EMPTY))
                .build()
            return MediaSession.ConnectionResult.AcceptedResultBuilder(session)
                .setAvailableSessionCommands(sessionCommands)
                .build()
        }

        override fun onCustomCommand(
            session: MediaSession,
            controller: MediaSession.ControllerInfo,
            customCommand: SessionCommand,
            args: Bundle,
        ): ListenableFuture<SessionResult> {
            if (customCommand.customAction == CMD_SET_EQ) {
                val presetId = args.getString(ARG_EQ_PRESET)
                applyEqPreset(EqPreset.fromId(presetId))
                return Futures.immediateFuture(SessionResult(SessionResult.RESULT_SUCCESS))
            }
            return super.onCustomCommand(session, controller, customCommand, args)
        }

        override fun onGetLibraryRoot(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            params: LibraryParams?,
        ): ListenableFuture<LibraryResult<MediaItem>> {
            val root = MediaItem.Builder()
                .setMediaId(ROOT_ID)
                .setMediaMetadata(
                    MediaMetadata.Builder()
                        .setTitle("Aether Music")
                        .setIsBrowsable(true)
                        .setIsPlayable(false)
                        .build()
                )
                .build()
            return Futures.immediateFuture(LibraryResult.ofItem(root, params))
        }

        override fun onGetChildren(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            parentId: String,
            page: Int,
            pageSize: Int,
            params: LibraryParams?,
        ): ListenableFuture<LibraryResult<ImmutableList<MediaItem>>> {
            val items: List<MediaItem> = when (parentId) {
                ROOT_ID -> listOf(
                    folderItem(NODE_FAVORITES, "Любимые треки"),
                    folderItem(NODE_HISTORY, "Недавние"),
                    folderItem(NODE_QUEUE, "Текущая очередь"),
                )
                NODE_FAVORITES -> library.current.favorites.map { it.toMediaItem() }
                NODE_HISTORY -> library.current.history.map { it.toMediaItem() }
                NODE_QUEUE -> library.current.lastQueue.map { it.toMediaItem() }
                else -> emptyList()
            }
            return Futures.immediateFuture(
                LibraryResult.ofItemList(ImmutableList.copyOf(items), params)
            )
        }

        override fun onGetItem(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            mediaId: String,
        ): ListenableFuture<LibraryResult<MediaItem>> {
            val st = library.current
            val track = (st.favorites + st.history + st.lastQueue).firstOrNull { it.videoId == mediaId }
            return if (track != null) {
                Futures.immediateFuture(LibraryResult.ofItem(track.toMediaItem(), null))
            } else {
                Futures.immediateFuture(LibraryResult.ofError(LibraryResult.RESULT_ERROR_BAD_VALUE))
            }
        }

        override fun onAddMediaItems(
            mediaSession: MediaSession,
            controller: MediaSession.ControllerInfo,
            mediaItems: MutableList<MediaItem>,
        ): ListenableFuture<MutableList<MediaItem>> {
            val st = library.current
            val known = (st.favorites + st.history + st.lastQueue).associateBy { it.videoId }
            val resolved = mediaItems.map { item ->
                if (item.localConfiguration != null) {
                    item
                } else {
                    known[item.mediaId]?.toMediaItem() ?: item
                }
            }.toMutableList()
            return Futures.immediateFuture(resolved)
        }

        override fun onPlaybackResumption(
            mediaSession: MediaSession,
            controller: MediaSession.ControllerInfo,
        ): ListenableFuture<MediaSession.MediaItemsWithStartPosition> {
            val st = library.current
            val resumeList = when {
                st.lastQueue.isNotEmpty() -> st.lastQueue
                st.favorites.isNotEmpty() -> st.favorites
                else -> st.history
            }.map { it.toMediaItem() }

            val startIndex = if (resumeList.isEmpty()) C.INDEX_UNSET else 0
            return Futures.immediateFuture(
                MediaSession.MediaItemsWithStartPosition(resumeList, startIndex, C.TIME_UNSET)
            )
        }

        private fun folderItem(id: String, title: String): MediaItem =
            MediaItem.Builder()
                .setMediaId(id)
                .setMediaMetadata(
                    MediaMetadata.Builder()
                        .setTitle(title)
                        .setIsBrowsable(true)
                        .setIsPlayable(false)
                        .build()
                )
                .build()
    }

    companion object {
        const val ROOT_ID = "aether_root"
        const val NODE_FAVORITES = "aether_favorites"
        const val NODE_HISTORY = "aether_history"
        const val NODE_QUEUE = "aether_queue"
        const val CMD_SET_EQ = "com.aether.music.CMD_SET_EQ"
        const val ARG_EQ_PRESET = "eq_preset"
    }
}
