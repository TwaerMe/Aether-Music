package com.aether.music.ui

import android.content.ComponentName
import android.content.Context
import android.os.Bundle
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionCommand
import androidx.media3.session.SessionToken
import com.aether.music.AetherApplication
import com.aether.music.data.EqPreset
import com.aether.music.data.Track
import com.aether.music.playback.AetherPlaybackService
import com.aether.music.playback.WaveEngine
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.guava.await
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class PlaybackUiState(
    val isConnected: Boolean = false,
    val currentTrack: Track? = null,
    val isPlaying: Boolean = false,
    val isBuffering: Boolean = false,
    val positionMs: Long = 0L,
    val durationMs: Long = 0L,
    val queue: List<Track> = emptyList(),
    val currentIndex: Int = -1,
    val shuffleEnabled: Boolean = false,
    val repeatMode: Int = Player.REPEAT_MODE_OFF,
    val waveActive: Boolean = false,
    val waveLoading: Boolean = false,
    val waveStatus: String = "Персональный поток на основе ваших лайков",
    val searchQuery: String = "",
    val searchLoading: Boolean = false,
    val searchResults: List<Track> = emptyList(),
    val errorMessage: String? = null,
)

class AetherPlayerController(private val appContext: Context) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val app = AetherApplication.instance
    private val store = app.libraryStore
    private val youtube = app.youtubeClient
    private val waveEngine = WaveEngine(youtube, store)

    private var controller: MediaController? = null
    private var progressJob: Job? = null
    private var waveExtendInFlight = false

    private val _uiState = MutableStateFlow(PlaybackUiState())
    val uiState: StateFlow<PlaybackUiState> = _uiState.asStateFlow()

    val libraryState = store.state

    private val playerListener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) {
            syncFromPlayer(player)
            maybeExtendWave(player)
        }
    }

    fun connect() {
        if (controller != null) return
        scope.launch {
            try {
                val token = SessionToken(
                    appContext,
                    ComponentName(appContext, AetherPlaybackService::class.java),
                )
                val mc = MediaController.Builder(appContext, token).buildAsync().await()
                controller = mc
                mc.addListener(playerListener)
                syncFromPlayer(mc)
                _uiState.update { it.copy(isConnected = true) }
                startProgressTicker()
            } catch (e: Exception) {
                _uiState.update { it.copy(errorMessage = "Ошибка подключения плеера: ${e.localizedMessage}") }
            }
        }
    }

    fun release() {
        progressJob?.cancel()
        controller?.removeListener(playerListener)
        controller?.release()
        controller = null
        scope.cancel()
    }

    private fun startProgressTicker() {
        progressJob?.cancel()
        progressJob = scope.launch {
            while (isActive) {
                val mc = controller
                if (mc != null && mc.mediaItemCount > 0) {
                    val pos = mc.currentPosition.coerceAtLeast(0L)
                    val dur = mc.duration.let { if (it > 0L) it else (Track.fromMediaItem(mc.currentMediaItem)?.durationSec ?: 0L) * 1000L }
                    _uiState.update {
                        it.copy(
                            positionMs = pos,
                            durationMs = dur,
                            isPlaying = mc.isPlaying,
                            isBuffering = mc.playbackState == Player.STATE_BUFFERING,
                        )
                    }
                }
                delay(400L)
            }
        }
    }

    private fun syncFromPlayer(player: Player) {
        val count = player.mediaItemCount
        val items = ArrayList<Track>(count)
        for (i in 0 until count) {
            Track.fromMediaItem(player.getMediaItemAt(i))?.let { items += it }
        }
        val current = Track.fromMediaItem(player.currentMediaItem)
        val dur = player.duration.let {
            if (it > 0L) it else (current?.durationSec ?: 0L) * 1000L
        }
        _uiState.update {
            it.copy(
                currentTrack = current,
                isPlaying = player.isPlaying,
                isBuffering = player.playbackState == Player.STATE_BUFFERING,
                positionMs = player.currentPosition.coerceAtLeast(0L),
                durationMs = dur,
                queue = items,
                currentIndex = player.currentMediaItemIndex,
                shuffleEnabled = player.shuffleModeEnabled,
                repeatMode = player.repeatMode,
            )
        }
    }

    fun playTracks(tracks: List<Track>, startIndex: Int = 0, isWave: Boolean = false) {
        if (tracks.isEmpty()) return
        val mc = controller ?: return
        val mediaItems = tracks.map { it.toMediaItem() }
        val safeIndex = startIndex.coerceIn(0, mediaItems.lastIndex)
        mc.setMediaItems(mediaItems, safeIndex, 0L)
        mc.prepare()
        mc.play()
        store.saveLastQueue(tracks)
        _uiState.update { it.copy(waveActive = isWave, errorMessage = null) }
    }

    fun jumpToQueueIndex(index: Int) {
        val mc = controller ?: return
        if (index in 0 until mc.mediaItemCount) {
            mc.seekTo(index, 0L)
            mc.prepare()
            mc.play()
        }
    }

    fun togglePlayPause() {
        val mc = controller ?: return
        if (mc.mediaItemCount == 0) {
            startWave()
            return
        }
        if (mc.isPlaying) {
            mc.pause()
        } else {
            mc.play()
        }
    }

    fun nextTrack() {
        val mc = controller ?: return
        if (mc.hasNextMediaItem()) {
            mc.seekToNextMediaItem()
            mc.prepare()
            mc.play()
        } else if (_uiState.value.waveActive) {
            startWave()
        }
    }

    fun previousTrack() {
        val mc = controller ?: return
        if (mc.currentPosition > 4000L) {
            mc.seekTo(0L)
        } else if (mc.hasPreviousMediaItem()) {
            mc.seekToPreviousMediaItem()
            mc.prepare()
            mc.play()
        }
    }

    fun seekTo(positionMs: Long) {
        controller?.seekTo(positionMs.coerceAtLeast(0L))
    }

    fun toggleShuffle() {
        val mc = controller ?: return
        mc.shuffleModeEnabled = !mc.shuffleModeEnabled
    }

    fun cycleRepeatMode() {
        val mc = controller ?: return
        mc.repeatMode = when (mc.repeatMode) {
            Player.REPEAT_MODE_OFF -> Player.REPEAT_MODE_ALL
            Player.REPEAT_MODE_ALL -> Player.REPEAT_MODE_ONE
            else -> Player.REPEAT_MODE_OFF
        }
    }

    fun toggleFavorite(track: Track) {
        store.toggleFavorite(track)
    }

    fun dislikeAndSkip(track: Track) {
        store.dislikeTrack(track.videoId)
        if (_uiState.value.currentTrack?.videoId == track.videoId) {
            nextTrack()
        }
    }

    fun setWaveMood(mood: String) {
        store.setWaveMood(mood)
        if (_uiState.value.waveActive) {
            startWave(mood)
        }
    }

    fun startWave(moodOverride: String? = null) {
        val mood = moodOverride ?: store.current.waveMood
        if (_uiState.value.waveLoading) return
        _uiState.update {
            it.copy(
                waveLoading = true,
                waveStatus = "Подбираем треки для «Моей волны»...",
                errorMessage = null,
            )
        }
        scope.launch {
            val (tracks, reason) = withContext(Dispatchers.IO) {
                waveEngine.buildWaveTracks(mood = mood)
            }
            if (tracks.isEmpty()) {
                _uiState.update {
                    it.copy(
                        waveLoading = false,
                        waveStatus = "Не удалось загрузить волну. Проверьте сеть.",
                        errorMessage = "Не удалось получить треки с YouTube",
                    )
                }
            } else {
                _uiState.update {
                    it.copy(
                        waveLoading = false,
                        waveActive = true,
                        waveStatus = reason,
                    )
                }
                playTracks(tracks, startIndex = 0, isWave = true)
            }
        }
    }

    private fun maybeExtendWave(player: Player) {
        if (!_uiState.value.waveActive || waveExtendInFlight) return
        val remaining = player.mediaItemCount - 1 - player.currentMediaItemIndex
        if (remaining > 3 || player.mediaItemCount == 0) return

        waveExtendInFlight = true
        val existingIds = buildSet {
            for (i in 0 until player.mediaItemCount) {
                add(player.getMediaItemAt(i).mediaId)
            }
        }
        val mood = store.current.waveMood
        scope.launch {
            try {
                val (moreTracks, reason) = withContext(Dispatchers.IO) {
                    waveEngine.buildWaveTracks(mood = mood, excludeVideoIds = existingIds, desiredCount = 12)
                }
                val mc = controller
                if (mc != null && moreTracks.isNotEmpty() && _uiState.value.waveActive) {
                    mc.addMediaItems(moreTracks.map { it.toMediaItem() })
                    _uiState.update { it.copy(waveStatus = reason) }
                }
            } finally {
                waveExtendInFlight = false
            }
        }
    }

    fun updateSearchQuery(q: String) {
        _uiState.update { it.copy(searchQuery = q) }
    }

    fun performSearch(queryOverride: String? = null) {
        val q = (queryOverride ?: _uiState.value.searchQuery).trim()
        if (q.isEmpty()) return
        store.addRecentSearch(q)
        _uiState.update { it.copy(searchQuery = q, searchLoading = true, errorMessage = null) }
        scope.launch {
            val results = withContext(Dispatchers.IO) {
                youtube.searchTracks(q, limit = 24)
            }
            _uiState.update {
                it.copy(
                    searchLoading = false,
                    searchResults = results,
                    errorMessage = if (results.isEmpty()) "Ничего не найдено или нет доступа к YouTube" else null,
                )
            }
        }
    }

    fun setEqPreset(preset: EqPreset) {
        store.setEqPreset(preset)
        val mc = controller ?: return
        val args = Bundle().apply { putString(AetherPlaybackService.ARG_EQ_PRESET, preset.id) }
        mc.sendCustomCommand(SessionCommand(AetherPlaybackService.CMD_SET_EQ, Bundle.EMPTY), args)
    }

    fun clearError() {
        _uiState.update { it.copy(errorMessage = null) }
    }
}
