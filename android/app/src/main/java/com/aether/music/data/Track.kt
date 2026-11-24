package com.aether.music.data

import android.net.Uri
import android.os.Bundle
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import org.json.JSONObject

data class Track(
    val videoId: String,
    val title: String,
    val artist: String,
    val durationSec: Long = 0L,
    val thumbnailUrl: String = defaultThumbnail(videoId),
) {
    val uid: String get() = "yt:$videoId"

    fun toMediaItem(): MediaItem {
        val extras = Bundle().apply {
            putString(EXTRA_VIDEO_ID, videoId)
            putLong(EXTRA_DURATION_SEC, durationSec)
            putString(EXTRA_THUMBNAIL_URL, thumbnailUrl)
        }
        val metadata = MediaMetadata.Builder()
            .setTitle(title)
            .setArtist(artist.ifBlank { "Aether Music" })
            .setAlbumTitle("Aether Music")
            .setDisplayTitle(title)
            .setSubtitle(artist.ifBlank { "Aether Music" })
            .setArtworkUri(Uri.parse(thumbnailUrl))
            .setIsBrowsable(false)
            .setIsPlayable(true)
            .setExtras(extras)
            .build()

        return MediaItem.Builder()
            .setMediaId(videoId)
            .setUri(Uri.parse("$SCHEME://$videoId"))
            .setMediaMetadata(metadata)
            .build()
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("videoId", videoId)
        put("title", title)
        put("artist", artist)
        put("durationSec", durationSec)
        put("thumbnailUrl", thumbnailUrl)
    }

    companion object {
        const val SCHEME = "aether-track"
        const val EXTRA_VIDEO_ID = "aether_video_id"
        const val EXTRA_DURATION_SEC = "aether_duration_sec"
        const val EXTRA_THUMBNAIL_URL = "aether_thumb_url"

        fun defaultThumbnail(videoId: String): String =
            "https://i.ytimg.com/vi/$videoId/hqdefault.jpg"

        fun fromJson(obj: JSONObject?): Track? {
            if (obj == null) return null
            val vid = obj.optString("videoId").trim()
            if (vid.isEmpty()) return null
            val title = obj.optString("title", "Без названия").ifBlank { "Без названия" }
            val artist = obj.optString("artist", "Неизвестный исполнитель").ifBlank { "Неизвестный исполнитель" }
            val duration = obj.optLong("durationSec", 0L).coerceAtLeast(0L)
            val thumb = obj.optString("thumbnailUrl").ifBlank { defaultThumbnail(vid) }
            return Track(
                videoId = vid,
                title = title,
                artist = artist,
                durationSec = duration,
                thumbnailUrl = thumb,
            )
        }

        fun fromMediaItem(item: MediaItem?): Track? {
            if (item == null) return null
            val vid = item.mediaId.ifBlank {
                item.mediaMetadata.extras?.getString(EXTRA_VIDEO_ID).orEmpty()
            }.trim()
            if (vid.isEmpty()) return null
            val md = item.mediaMetadata
            val title = md.title?.toString()?.ifBlank { "Без названия" } ?: "Без названия"
            val artist = md.artist?.toString()?.ifBlank { "Aether Music" } ?: "Aether Music"
            val duration = md.extras?.getLong(EXTRA_DURATION_SEC, 0L) ?: 0L
            val thumb = md.artworkUri?.toString()?.ifBlank { null }
                ?: md.extras?.getString(EXTRA_THUMBNAIL_URL)?.ifBlank { null }
                ?: defaultThumbnail(vid)
            return Track(
                videoId = vid,
                title = title,
                artist = artist,
                durationSec = duration,
                thumbnailUrl = thumb,
            )
        }
    }
}

enum class EqPreset(val id: String, val label: String) {
    FLAT("flat", "EQ: Ровный"),
    BASS("bass", "EQ: Бас"),
    VOCAL("vocal", "EQ: Вокал"),
    NIGHT("night", "EQ: Ночной"),
    DRIVE("drive", "EQ: Драйв");

    companion object {
        fun fromId(id: String?): EqPreset =
            entries.firstOrNull { it.id == id } ?: FLAT
    }
}

object WaveMoods {
    val all = listOf("Любое", "Спокойное", "Бодрое", "Грустное", "Ночной драйв", "Фокус")

    val modifiers = mapOf(
        "Любое" to "",
        "Спокойное" to "chill calm acoustic",
        "Бодрое" to "upbeat energetic workout",
        "Грустное" to "sad melancholic indie",
        "Ночной драйв" to "night drive synthwave phonk",
        "Фокус" to "lofi instrumental focus study",
    )

    val fallbackQueries = mapOf(
        "Любое" to listOf("indie pop hits", "top music hits 2025", "alternative rock hits"),
        "Спокойное" to listOf("chill acoustic indie playlist", "ambient downtempo chill"),
        "Бодрое" to listOf("energetic electronic dance hits", "upbeat workout music"),
        "Грустное" to listOf("sad indie alternative songs", "melancholic acoustic ballad"),
        "Ночной драйв" to listOf("night drive synthwave retrowave", "dark phonk night drive"),
        "Фокус" to listOf("lofi beats study focus", "instrumental ambient focus music"),
    )
}

fun formatDuration(seconds: Long): String {
    val s = seconds.coerceAtLeast(0L)
    val hrs = s / 3600L
    val mins = (s % 3600L) / 60L
    val secs = s % 60L
    return if (hrs > 0L) {
        "%d:%02d:%02d".format(hrs, mins, secs)
    } else {
        "%02d:%02d".format(mins, secs)
    }
}
