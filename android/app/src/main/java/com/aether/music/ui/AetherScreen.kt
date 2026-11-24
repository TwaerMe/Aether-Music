package com.aether.music.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.Favorite
import androidx.compose.material.icons.rounded.FavoriteBorder
import androidx.compose.material.icons.rounded.GraphicEq
import androidx.compose.material.icons.rounded.KeyboardArrowDown
import androidx.compose.material.icons.rounded.LibraryMusic
import androidx.compose.material.icons.rounded.MusicNote
import androidx.compose.material.icons.rounded.Pause
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material.icons.rounded.QueueMusic
import androidx.compose.material.icons.rounded.Repeat
import androidx.compose.material.icons.rounded.RepeatOne
import androidx.compose.material.icons.rounded.Search
import androidx.compose.material.icons.rounded.Shuffle
import androidx.compose.material.icons.rounded.SkipNext
import androidx.compose.material.icons.rounded.SkipPrevious
import androidx.compose.material.icons.rounded.ThumbDownOffAlt
import androidx.compose.material.icons.rounded.Waves
import androidx.compose.material.icons.rounded.Widgets
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.media3.common.Player
import coil.compose.AsyncImage
import com.aether.music.BuildConfig
import com.aether.music.data.EqPreset
import com.aether.music.data.Track
import com.aether.music.data.UserLibraryState
import com.aether.music.data.WaveMoods
import com.aether.music.data.formatDuration
import com.aether.music.ui.theme.AetherColors

private enum class AetherTab(val label: String, val icon: ImageVector) {
    WAVE("Волна", Icons.Rounded.Waves),
    SEARCH("Поиск", Icons.Rounded.Search),
    LIBRARY("Коллекция", Icons.Rounded.LibraryMusic),
    QUEUE("Очередь", Icons.Rounded.QueueMusic),
}

@Composable
fun AetherAppScreen(controller: AetherPlayerController) {
    val playback by controller.uiState.collectAsState()
    val library by controller.libraryState.collectAsState()
    var currentTab by remember { mutableStateOf(AetherTab.WAVE) }
    var showFullPlayer by remember { mutableStateOf(false) }
    var showWidgetGuide by remember { mutableStateOf(false) }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(AetherColors.BgRoot)
    ) {
        Scaffold(
            containerColor = AetherColors.BgRoot,
            topBar = {
                AetherTopBar(
                    onWidgetBadgeClick = { showWidgetGuide = true }
                )
            },
            bottomBar = {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(AetherColors.BgNav)
                        .navigationBarsPadding()
                ) {
                    if (playback.currentTrack != null) {
                        MiniPlayerBar(
                            playback = playback,
                            isFavorite = library.favorites.any { it.videoId == playback.currentTrack?.videoId },
                            onOpenFull = { showFullPlayer = true },
                            onTogglePlay = { controller.togglePlayPause() },
                            onNext = { controller.nextTrack() },
                            onToggleFavorite = { playback.currentTrack?.let { controller.toggleFavorite(it) } },
                        )
                    }
                    HorizontalDivider(color = AetherColors.Border, thickness = 1.dp)
                    BottomNavRow(
                        currentTab = currentTab,
                        onSelectTab = { currentTab = it },
                    )
                }
            },
        ) { innerPadding ->
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding)
            ) {
                when (currentTab) {
                    AetherTab.WAVE -> WaveTabScreen(
                        playback = playback,
                        library = library,
                        onMoodSelect = { controller.setWaveMood(it) },
                        onStartWave = { controller.startWave() },
                        onTogglePlay = { controller.togglePlayPause() },
                        onTrackClick = { list, idx -> controller.playTracks(list, idx, isWave = false) },
                        onToggleFav = { controller.toggleFavorite(it) },
                        onShowWidgetHelp = { showWidgetGuide = true },
                    )
                    AetherTab.SEARCH -> SearchTabScreen(
                        playback = playback,
                        library = library,
                        onQueryChange = { controller.updateSearchQuery(it) },
                        onSearch = { controller.performSearch(it) },
                        onTrackClick = { list, idx -> controller.playTracks(list, idx, isWave = false) },
                        onToggleFav = { controller.toggleFavorite(it) },
                    )
                    AetherTab.LIBRARY -> LibraryTabScreen(
                        playback = playback,
                        library = library,
                        onTrackClick = { list, idx -> controller.playTracks(list, idx, isWave = false) },
                        onToggleFav = { controller.toggleFavorite(it) },
                    )
                    AetherTab.QUEUE -> QueueTabScreen(
                        playback = playback,
                        library = library,
                        onJumpIndex = { controller.jumpToQueueIndex(it) },
                        onToggleShuffle = { controller.toggleShuffle() },
                        onCycleRepeat = { controller.cycleRepeatMode() },
                        onSelectEq = { controller.setEqPreset(it) },
                        onToggleFav = { controller.toggleFavorite(it) },
                    )
                }

                playback.errorMessage?.let { err ->
                    Surface(
                        modifier = Modifier
                            .align(Alignment.BottomCenter)
                            .padding(16.dp)
                            .fillMaxWidth(),
                        color = AetherColors.BgCardAlt,
                        shape = RoundedCornerShape(12.dp),
                        border = BorderStroke(1.dp, AetherColors.LikeRed.copy(alpha = 0.6f)),
                    ) {
                        Row(
                            modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                text = err,
                                color = AetherColors.TextMain,
                                fontSize = 13.sp,
                                modifier = Modifier.weight(1f),
                            )
                            IconButton(
                                onClick = { controller.clearError() },
                                modifier = Modifier.size(28.dp),
                            ) {
                                Icon(
                                    imageVector = Icons.Rounded.Close,
                                    contentDescription = "Закрыть",
                                    tint = AetherColors.TextMuted,
                                    modifier = Modifier.size(18.dp),
                                )
                            }
                        }
                    }
                }
            }
        }

        if (showFullPlayer && playback.currentTrack != null) {
            FullScreenPlayerOverlay(
                playback = playback,
                library = library,
                onDismiss = { showFullPlayer = false },
                onTogglePlay = { controller.togglePlayPause() },
                onNext = { controller.nextTrack() },
                onPrevious = { controller.previousTrack() },
                onSeek = { controller.seekTo(it) },
                onToggleFav = { playback.currentTrack?.let { controller.toggleFavorite(it) } },
                onDislike = { playback.currentTrack?.let { controller.dislikeAndSkip(it) } },
                onToggleShuffle = { controller.toggleShuffle() },
                onCycleRepeat = { controller.cycleRepeatMode() },
                onSelectEq = { controller.setEqPreset(it) },
            )
        }

        if (showWidgetGuide) {
            OriginOsWidgetDialog(onDismiss = { showWidgetGuide = false })
        }
    }
}

@Composable
private fun AetherTopBar(onWidgetBadgeClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(AetherColors.BgRoot)
            .statusBarsPadding()
            .padding(horizontal = 20.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = "Aether",
            color = AetherColors.TextMain,
            fontSize = 22.sp,
            fontWeight = FontWeight.Bold,
        )
        Text(
            text = " music",
            color = AetherColors.TextMuted,
            fontSize = 22.sp,
            fontWeight = FontWeight.Normal,
        )
        Spacer(modifier = Modifier.weight(1f))
        Surface(
            onClick = onWidgetBadgeClick,
            shape = RoundedCornerShape(8.dp),
            color = AetherColors.BgCard,
            border = BorderStroke(
                1.dp,
                if (BuildConfig.IS_ORIGIN_OS_WIDGET_BUILD) AetherColors.WaveGold.copy(alpha = 0.55f) else AetherColors.Border
            ),
        ) {
            Row(
                modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Icon(
                    imageVector = Icons.Rounded.Widgets,
                    contentDescription = "OriginOS Widget",
                    tint = if (BuildConfig.IS_ORIGIN_OS_WIDGET_BUILD) AetherColors.WaveGold else AetherColors.TextMuted,
                    modifier = Modifier.size(15.dp),
                )
                Text(
                    text = if (BuildConfig.IS_ORIGIN_OS_WIDGET_BUILD) "OriginOS 6" else "MediaSession",
                    color = if (BuildConfig.IS_ORIGIN_OS_WIDGET_BUILD) AetherColors.WaveGold else AetherColors.TextMuted,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium,
                )
            }
        }
    }
}

@Composable
private fun BottomNavRow(
    currentTab: AetherTab,
    onSelectTab: (AetherTab) -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .height(62.dp)
            .padding(horizontal = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        AetherTab.entries.forEach { tab ->
            val selected = tab == currentTab
            val tint = if (selected) AetherColors.WaveGold else AetherColors.TextMuted
            Column(
                modifier = Modifier
                    .weight(1f)
                    .clip(RoundedCornerShape(10.dp))
                    .clickable { onSelectTab(tab) }
                    .padding(vertical = 8.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(3.dp),
            ) {
                Icon(
                    imageVector = tab.icon,
                    contentDescription = tab.label,
                    tint = tint,
                    modifier = Modifier.size(22.dp),
                )
                Text(
                    text = tab.label,
                    color = tint,
                    fontSize = 11.sp,
                    fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
                )
            }
        }
    }
}

@Composable
private fun WaveTabScreen(
    playback: PlaybackUiState,
    library: UserLibraryState,
    onMoodSelect: (String) -> Unit,
    onStartWave: () -> Unit,
    onTogglePlay: () -> Unit,
    onTrackClick: (List<Track>, Int) -> Unit,
    onToggleFav: (Track) -> Unit,
    onShowWidgetHelp: () -> Unit,
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 18.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item {
            Surface(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(16.dp),
                color = AetherColors.BgCard,
                border = BorderStroke(1.dp, AetherColors.Border),
            ) {
                Column(
                    modifier = Modifier.padding(20.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    Text(
                        text = "Моя волна",
                        color = AetherColors.TextMain,
                        fontSize = 24.sp,
                        fontWeight = FontWeight.Bold,
                    )
                    Text(
                        text = playback.waveStatus,
                        color = AetherColors.TextMuted,
                        fontSize = 13.sp,
                        lineHeight = 18.sp,
                    )
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Button(
                            onClick = {
                                if (playback.waveActive && playback.currentTrack != null) {
                                    onTogglePlay()
                                } else {
                                    onStartWave()
                                }
                            },
                            colors = ButtonDefaults.buttonColors(
                                containerColor = AetherColors.WaveGold,
                                contentColor = AetherColors.BgRoot,
                            ),
                            shape = RoundedCornerShape(12.dp),
                            contentPadding = PaddingValues(horizontal = 20.dp, vertical = 12.dp),
                        ) {
                            if (playback.waveLoading) {
                                CircularProgressIndicator(
                                    color = AetherColors.BgRoot,
                                    strokeWidth = 2.dp,
                                    modifier = Modifier.size(18.dp),
                                )
                                Spacer(modifier = Modifier.width(8.dp))
                                Text("Загрузка...", fontWeight = FontWeight.Bold)
                            } else {
                                Icon(
                                    imageVector = if (playback.waveActive && playback.isPlaying) Icons.Rounded.Pause else Icons.Rounded.PlayArrow,
                                    contentDescription = null,
                                    modifier = Modifier.size(20.dp),
                                )
                                Spacer(modifier = Modifier.width(6.dp))
                                Text(
                                    text = if (playback.waveActive && playback.isPlaying) "Пауза" else "Запустить волну",
                                    fontWeight = FontWeight.Bold,
                                )
                            }
                        }

                        if (playback.waveActive) {
                            TextButton(onClick = onStartWave) {
                                Text("Обновить", color = AetherColors.TextMuted)
                            }
                        }
                    }
                }
            }
        }

        item {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    text = "Настроение волны",
                    color = AetherColors.TextMuted,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(WaveMoods.all) { mood ->
                        val active = library.waveMood == mood
                        Surface(
                            onClick = { onMoodSelect(mood) },
                            shape = RoundedCornerShape(10.dp),
                            color = if (active) AetherColors.WaveSoft else AetherColors.BgCard,
                            border = BorderStroke(
                                1.dp,
                                if (active) AetherColors.WaveGold else AetherColors.Border
                            ),
                        ) {
                            Text(
                                text = mood,
                                color = if (active) AetherColors.WaveGold else AetherColors.TextMuted,
                                fontSize = 13.sp,
                                fontWeight = if (active) FontWeight.SemiBold else FontWeight.Normal,
                                modifier = Modifier.padding(horizontal = 14.dp, vertical = 8.dp),
                            )
                        }
                    }
                }
            }
        }

        item {
            Surface(
                onClick = onShowWidgetHelp,
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(12.dp),
                color = AetherColors.BgCardAlt,
                border = BorderStroke(1.dp, AetherColors.Border),
            ) {
                Row(
                    modifier = Modifier.padding(14.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    Icon(
                        imageVector = Icons.Rounded.Widgets,
                        contentDescription = null,
                        tint = AetherColors.WaveGold,
                        modifier = Modifier.size(22.dp),
                    )
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = if (BuildConfig.IS_ORIGIN_OS_WIDGET_BUILD) {
                                "Встроенный виджет OriginOS 6 активен"
                            } else {
                                "Системный медиа-плеер активен"
                            },
                            color = AetherColors.TextMain,
                            fontSize = 13.sp,
                            fontWeight = FontWeight.SemiBold,
                        )
                        Text(
                            text = if (BuildConfig.IS_ORIGIN_OS_WIDGET_BUILD) {
                                "В Атомном плеере выберите Spotify — подключится Aether Music"
                            } else {
                                "Работает на экране блокировки и в Origin Island"
                            },
                            color = AetherColors.TextMuted,
                            fontSize = 12.sp,
                        )
                    }
                }
            }
        }

        if (library.history.isNotEmpty()) {
            item {
                Text(
                    text = "Недавно слушали",
                    color = AetherColors.TextMain,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
            itemsIndexed(library.history.take(12), key = { _, t -> "hist_${t.videoId}" }) { idx, track ->
                TrackRow(
                    track = track,
                    isCurrent = playback.currentTrack?.videoId == track.videoId,
                    isFavorite = library.favorites.any { it.videoId == track.videoId },
                    onClick = { onTrackClick(library.history.take(25), idx) },
                    onToggleFav = { onToggleFav(track) },
                )
            }
        }
    }
}

@Composable
private fun SearchTabScreen(
    playback: PlaybackUiState,
    library: UserLibraryState,
    onQueryChange: (String) -> Unit,
    onSearch: (String?) -> Unit,
    onTrackClick: (List<Track>, Int) -> Unit,
    onToggleFav: (Track) -> Unit,
) {
    val keyboard = LocalSoftwareKeyboardController.current

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(horizontal = 18.dp),
    ) {
        OutlinedTextField(
            value = playback.searchQuery,
            onValueChange = onQueryChange,
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 8.dp),
            placeholder = { Text("Поиск треков и исполнителей...", color = AetherColors.TextDim) },
            singleLine = true,
            leadingIcon = {
                Icon(Icons.Rounded.Search, contentDescription = null, tint = AetherColors.TextMuted)
            },
            trailingIcon = {
                if (playback.searchQuery.isNotEmpty()) {
                    IconButton(onClick = { onQueryChange("") }) {
                        Icon(Icons.Rounded.Close, contentDescription = "Очистить", tint = AetherColors.TextMuted)
                    }
                }
            },
            colors = OutlinedTextFieldDefaults.colors(
                focusedContainerColor = AetherColors.BgCard,
                unfocusedContainerColor = AetherColors.BgCard,
                focusedBorderColor = AetherColors.WaveGold,
                unfocusedBorderColor = AetherColors.Border,
                focusedTextColor = AetherColors.TextMain,
                unfocusedTextColor = AetherColors.TextMain,
                cursorColor = AetherColors.WaveGold,
            ),
            shape = RoundedCornerShape(12.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            keyboardActions = KeyboardActions(
                onSearch = {
                    keyboard?.hide()
                    onSearch(null)
                }
            ),
        )

        if (playback.searchLoading) {
            LinearProgressIndicator(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 6.dp)
                    .clip(RoundedCornerShape(4.dp)),
                color = AetherColors.WaveGold,
                trackColor = AetherColors.BgCard,
            )
        }

        if (playback.searchResults.isEmpty() && !playback.searchLoading) {
            if (library.recentSearches.isNotEmpty()) {
                Text(
                    text = "Недавние запросы",
                    color = AetherColors.TextMuted,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.padding(top = 10.dp, bottom = 8.dp),
                )
                LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(library.recentSearches) { item ->
                        Surface(
                            onClick = {
                                keyboard?.hide()
                                onSearch(item)
                            },
                            modifier = Modifier.fillMaxWidth(),
                            shape = RoundedCornerShape(10.dp),
                            color = AetherColors.BgCard,
                            border = BorderStroke(1.dp, AetherColors.Border),
                        ) {
                            Row(
                                modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(10.dp),
                            ) {
                                Icon(Icons.Rounded.Search, contentDescription = null, tint = AetherColors.TextDim, modifier = Modifier.size(18.dp))
                                Text(item, color = AetherColors.TextMain, fontSize = 14.sp)
                            }
                        }
                    }
                }
            } else {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text(
                        text = "Введите название трека или артиста",
                        color = AetherColors.TextDim,
                        fontSize = 14.sp,
                    )
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                itemsIndexed(playback.searchResults, key = { _, t -> "sr_${t.videoId}" }) { idx, track ->
                    TrackRow(
                        track = track,
                        isCurrent = playback.currentTrack?.videoId == track.videoId,
                        isFavorite = library.favorites.any { it.videoId == track.videoId },
                        onClick = { onTrackClick(playback.searchResults, idx) },
                        onToggleFav = { onToggleFav(track) },
                    )
                }
            }
        }
    }
}

@Composable
private fun LibraryTabScreen(
    playback: PlaybackUiState,
    library: UserLibraryState,
    onTrackClick: (List<Track>, Int) -> Unit,
    onToggleFav: (Track) -> Unit,
) {
    var section by remember { mutableIntStateOf(0) } // 0 = Любимые, 1 = История

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(horizontal = 18.dp),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            listOf(
                0 to "Любимые (${library.favorites.size})",
                1 to "История (${library.history.size})",
            ).forEach { (idx, title) ->
                val active = section == idx
                Surface(
                    onClick = { section = idx },
                    modifier = Modifier.weight(1f),
                    shape = RoundedCornerShape(10.dp),
                    color = if (active) AetherColors.WaveSoft else AetherColors.BgCard,
                    border = BorderStroke(1.dp, if (active) AetherColors.WaveGold else AetherColors.Border),
                ) {
                    Box(
                        modifier = Modifier.padding(vertical = 10.dp),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            text = title,
                            color = if (active) AetherColors.WaveGold else AetherColors.TextMuted,
                            fontSize = 13.sp,
                            fontWeight = FontWeight.SemiBold,
                        )
                    }
                }
            }
        }

        val list = if (section == 0) library.favorites else library.history
        if (list.isEmpty()) {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    text = if (section == 0) "Пока нет любимых треков" else "История прослушиваний пуста",
                    color = AetherColors.TextDim,
                    fontSize = 14.sp,
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                itemsIndexed(list, key = { _, t -> "lib_${section}_${t.videoId}" }) { idx, track ->
                    TrackRow(
                        track = track,
                        isCurrent = playback.currentTrack?.videoId == track.videoId,
                        isFavorite = library.favorites.any { it.videoId == track.videoId },
                        onClick = { onTrackClick(list, idx) },
                        onToggleFav = { onToggleFav(track) },
                    )
                }
            }
        }
    }
}

@Composable
private fun QueueTabScreen(
    playback: PlaybackUiState,
    library: UserLibraryState,
    onJumpIndex: (Int) -> Unit,
    onToggleShuffle: () -> Unit,
    onCycleRepeat: () -> Unit,
    onSelectEq: (EqPreset) -> Unit,
    onToggleFav: (Track) -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(horizontal = 18.dp),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                text = "Очередь (${playback.queue.size})",
                color = AetherColors.TextMain,
                fontSize = 16.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.weight(1f),
            )
            IconButton(onClick = onToggleShuffle) {
                Icon(
                    imageVector = Icons.Rounded.Shuffle,
                    contentDescription = "Перемешать",
                    tint = if (playback.shuffleEnabled) AetherColors.WaveGold else AetherColors.TextMuted,
                )
            }
            IconButton(onClick = onCycleRepeat) {
                Icon(
                    imageVector = if (playback.repeatMode == Player.REPEAT_MODE_ONE) Icons.Rounded.RepeatOne else Icons.Rounded.Repeat,
                    contentDescription = "Повтор",
                    tint = if (playback.repeatMode != Player.REPEAT_MODE_OFF) AetherColors.WaveGold else AetherColors.TextMuted,
                )
            }
        }

        LazyRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.padding(bottom = 10.dp),
        ) {
            items(EqPreset.entries) { preset ->
                val active = library.eqPreset == preset
                Surface(
                    onClick = { onSelectEq(preset) },
                    shape = RoundedCornerShape(10.dp),
                    color = if (active) AetherColors.WaveSoft else AetherColors.BgCard,
                    border = BorderStroke(1.dp, if (active) AetherColors.WaveGold else AetherColors.Border),
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(5.dp),
                    ) {
                        Icon(
                            imageVector = Icons.Rounded.GraphicEq,
                            contentDescription = null,
                            tint = if (active) AetherColors.WaveGold else AetherColors.TextMuted,
                            modifier = Modifier.size(15.dp),
                        )
                        Text(
                            text = preset.label,
                            color = if (active) AetherColors.WaveGold else AetherColors.TextMuted,
                            fontSize = 12.sp,
                        )
                    }
                }
            }
        }

        if (playback.queue.isEmpty()) {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("Очередь пуста — запустите волну или поиск", color = AetherColors.TextDim, fontSize = 14.sp)
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(vertical = 6.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                itemsIndexed(playback.queue, key = { idx, t -> "q_${idx}_${t.videoId}" }) { idx, track ->
                    TrackRow(
                        track = track,
                        isCurrent = idx == playback.currentIndex,
                        isFavorite = library.favorites.any { it.videoId == track.videoId },
                        onClick = { onJumpIndex(idx) },
                        onToggleFav = { onToggleFav(track) },
                    )
                }
            }
        }
    }
}

@Composable
private fun TrackRow(
    track: Track,
    isCurrent: Boolean,
    isFavorite: Boolean,
    onClick: () -> Unit,
    onToggleFav: () -> Unit,
) {
    Surface(
        onClick = onClick,
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        color = if (isCurrent) AetherColors.BgCardAlt else AetherColors.BgCard,
        border = BorderStroke(
            1.dp,
            if (isCurrent) AetherColors.WaveGold.copy(alpha = 0.65f) else AetherColors.Border
        ),
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 9.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Box(
                modifier = Modifier
                    .size(48.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(AetherColors.BgHover),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    imageVector = Icons.Rounded.MusicNote,
                    contentDescription = null,
                    tint = AetherColors.TextDim,
                    modifier = Modifier.size(22.dp),
                )
                AsyncImage(
                    model = track.thumbnailUrl,
                    contentDescription = track.title,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }

            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = track.title,
                    color = if (isCurrent) AetherColors.WaveGold else AetherColors.TextMain,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(modifier = Modifier.height(2.dp))
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    Text(
                        text = track.artist,
                        color = AetherColors.TextMuted,
                        fontSize = 12.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f, fill = false),
                    )
                    if (track.durationSec > 0L) {
                        Text(
                            text = "· ${formatDuration(track.durationSec)}",
                            color = AetherColors.TextDim,
                            fontSize = 12.sp,
                        )
                    }
                }
            }

            IconButton(
                onClick = onToggleFav,
                modifier = Modifier.size(36.dp),
            ) {
                Icon(
                    imageVector = if (isFavorite) Icons.Rounded.Favorite else Icons.Rounded.FavoriteBorder,
                    contentDescription = "Любимое",
                    tint = if (isFavorite) AetherColors.LikeRed else AetherColors.TextDim,
                    modifier = Modifier.size(20.dp),
                )
            }
        }
    }
}

@Composable
private fun MiniPlayerBar(
    playback: PlaybackUiState,
    isFavorite: Boolean,
    onOpenFull: () -> Unit,
    onTogglePlay: () -> Unit,
    onNext: () -> Unit,
    onToggleFavorite: () -> Unit,
) {
    val track = playback.currentTrack ?: return
    val progress = if (playback.durationMs > 0L) {
        (playback.positionMs.toFloat() / playback.durationMs.toFloat()).coerceIn(0f, 1f)
    } else 0f

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(AetherColors.BgBar)
            .clickable { onOpenFull() },
    ) {
        LinearProgressIndicator(
            progress = { progress },
            modifier = Modifier
                .fillMaxWidth()
                .height(2.dp),
            color = AetherColors.WaveGold,
            trackColor = AetherColors.Border,
        )
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            AsyncImage(
                model = track.thumbnailUrl,
                contentDescription = track.title,
                contentScale = ContentScale.Crop,
                modifier = Modifier
                    .size(44.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(AetherColors.BgCard),
            )
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = track.title,
                    color = AetherColors.TextMain,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    text = track.artist,
                    color = AetherColors.TextMuted,
                    fontSize = 11.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            IconButton(onClick = onToggleFavorite, modifier = Modifier.size(34.dp)) {
                Icon(
                    imageVector = if (isFavorite) Icons.Rounded.Favorite else Icons.Rounded.FavoriteBorder,
                    contentDescription = "Лайк",
                    tint = if (isFavorite) AetherColors.LikeRed else AetherColors.TextMuted,
                    modifier = Modifier.size(19.dp),
                )
            }
            IconButton(onClick = onTogglePlay, modifier = Modifier.size(38.dp)) {
                if (playback.isBuffering) {
                    CircularProgressIndicator(
                        color = AetherColors.WaveGold,
                        strokeWidth = 2.dp,
                        modifier = Modifier.size(20.dp),
                    )
                } else {
                    Icon(
                        imageVector = if (playback.isPlaying) Icons.Rounded.Pause else Icons.Rounded.PlayArrow,
                        contentDescription = "Играть/Пауза",
                        tint = AetherColors.TextMain,
                        modifier = Modifier.size(26.dp),
                    )
                }
            }
            IconButton(onClick = onNext, modifier = Modifier.size(34.dp)) {
                Icon(
                    imageVector = Icons.Rounded.SkipNext,
                    contentDescription = "Следующий",
                    tint = AetherColors.TextMain,
                    modifier = Modifier.size(22.dp),
                )
            }
        }
    }
}

@Composable
private fun FullScreenPlayerOverlay(
    playback: PlaybackUiState,
    library: UserLibraryState,
    onDismiss: () -> Unit,
    onTogglePlay: () -> Unit,
    onNext: () -> Unit,
    onPrevious: () -> Unit,
    onSeek: (Long) -> Unit,
    onToggleFav: () -> Unit,
    onDislike: () -> Unit,
    onToggleShuffle: () -> Unit,
    onCycleRepeat: () -> Unit,
    onSelectEq: (EqPreset) -> Unit,
) {
    val track = playback.currentTrack ?: return
    val isFavorite = library.favorites.any { it.videoId == track.videoId }
    var draggingPos by remember(track.videoId) { mutableStateOf<Float?>(null) }
    val durationFloat = playback.durationMs.coerceAtLeast(1L).toFloat()
    val sliderVal = draggingPos ?: (playback.positionMs.toFloat() / durationFloat).coerceIn(0f, 1f)

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(AetherColors.BgRoot)
            .statusBarsPadding()
            .navigationBarsPadding()
            .padding(horizontal = 24.dp, vertical = 12.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            IconButton(onClick = onDismiss) {
                Icon(
                    imageVector = Icons.Rounded.KeyboardArrowDown,
                    contentDescription = "Свернуть",
                    tint = AetherColors.TextMain,
                    modifier = Modifier.size(30.dp),
                )
            }
            Spacer(modifier = Modifier.weight(1f))
            Text(
                text = if (playback.waveActive) "Моя волна" else "Сейчас играет",
                color = AetherColors.TextMuted,
                fontSize = 13.sp,
                fontWeight = FontWeight.Medium,
            )
            Spacer(modifier = Modifier.weight(1f))
            Spacer(modifier = Modifier.size(48.dp))
        }

        Spacer(modifier = Modifier.height(16.dp))

        AsyncImage(
            model = track.thumbnailUrl,
            contentDescription = track.title,
            contentScale = ContentScale.Crop,
            modifier = Modifier
                .fillMaxWidth(0.88f)
                .aspectRatio(1f)
                .clip(RoundedCornerShape(18.dp))
                .background(AetherColors.BgCard)
                .border(1.dp, AetherColors.Border, RoundedCornerShape(18.dp)),
        )

        Spacer(modifier = Modifier.height(26.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = track.title,
                    color = AetherColors.TextMain,
                    fontSize = 20.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = track.artist,
                    color = AetherColors.TextMuted,
                    fontSize = 14.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            IconButton(onClick = onDislike) {
                Icon(
                    imageVector = Icons.Rounded.ThumbDownOffAlt,
                    contentDescription = "Не нравится",
                    tint = AetherColors.TextMuted,
                )
            }
            IconButton(onClick = onToggleFav) {
                Icon(
                    imageVector = if (isFavorite) Icons.Rounded.Favorite else Icons.Rounded.FavoriteBorder,
                    contentDescription = "Любимое",
                    tint = if (isFavorite) AetherColors.LikeRed else AetherColors.TextMuted,
                )
            }
        }

        Spacer(modifier = Modifier.height(14.dp))

        Slider(
            value = sliderVal,
            onValueChange = { draggingPos = it },
            onValueChangeFinished = {
                draggingPos?.let { frac -> onSeek((frac * durationFloat).toLong()) }
                draggingPos = null
            },
            colors = SliderDefaults.colors(
                thumbColor = AetherColors.WaveGold,
                activeTrackColor = AetherColors.WaveGold,
                inactiveTrackColor = AetherColors.Border,
            ),
            modifier = Modifier.fillMaxWidth(),
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            val shownMs = if (draggingPos != null) (draggingPos!! * durationFloat).toLong() else playback.positionMs
            Text(formatDuration(shownMs / 1000L), color = AetherColors.TextDim, fontSize = 12.sp)
            Text(formatDuration(playback.durationMs / 1000L), color = AetherColors.TextDim, fontSize = 12.sp)
        }

        Spacer(modifier = Modifier.height(16.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceEvenly,
        ) {
            IconButton(onClick = onToggleShuffle) {
                Icon(
                    imageVector = Icons.Rounded.Shuffle,
                    contentDescription = "Перемешать",
                    tint = if (playback.shuffleEnabled) AetherColors.WaveGold else AetherColors.TextMuted,
                )
            }
            IconButton(onClick = onPrevious, modifier = Modifier.size(52.dp)) {
                Icon(
                    imageVector = Icons.Rounded.SkipPrevious,
                    contentDescription = "Предыдущий",
                    tint = AetherColors.TextMain,
                    modifier = Modifier.size(34.dp),
                )
            }
            Surface(
                onClick = onTogglePlay,
                shape = CircleShape,
                color = AetherColors.WaveGold,
                modifier = Modifier.size(66.dp),
            ) {
                Box(contentAlignment = Alignment.Center) {
                    if (playback.isBuffering) {
                        CircularProgressIndicator(
                            color = AetherColors.BgRoot,
                            strokeWidth = 3.dp,
                            modifier = Modifier.size(28.dp),
                        )
                    } else {
                        Icon(
                            imageVector = if (playback.isPlaying) Icons.Rounded.Pause else Icons.Rounded.PlayArrow,
                            contentDescription = "Играть/Пауза",
                            tint = AetherColors.BgRoot,
                            modifier = Modifier.size(36.dp),
                        )
                    }
                }
            }
            IconButton(onClick = onNext, modifier = Modifier.size(52.dp)) {
                Icon(
                    imageVector = Icons.Rounded.SkipNext,
                    contentDescription = "Следующий",
                    tint = AetherColors.TextMain,
                    modifier = Modifier.size(34.dp),
                )
            }
            IconButton(onClick = onCycleRepeat) {
                Icon(
                    imageVector = if (playback.repeatMode == Player.REPEAT_MODE_ONE) Icons.Rounded.RepeatOne else Icons.Rounded.Repeat,
                    contentDescription = "Повтор",
                    tint = if (playback.repeatMode != Player.REPEAT_MODE_OFF) AetherColors.WaveGold else AetherColors.TextMuted,
                )
            }
        }

        Spacer(modifier = Modifier.weight(1f))

        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(EqPreset.entries) { preset ->
                val active = library.eqPreset == preset
                Surface(
                    onClick = { onSelectEq(preset) },
                    shape = RoundedCornerShape(10.dp),
                    color = if (active) AetherColors.WaveSoft else AetherColors.BgCard,
                    border = BorderStroke(1.dp, if (active) AetherColors.WaveGold else AetherColors.Border),
                ) {
                    Text(
                        text = preset.label,
                        color = if (active) AetherColors.WaveGold else AetherColors.TextMuted,
                        fontSize = 12.sp,
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
                    )
                }
            }
        }
    }
}

@Composable
private fun OriginOsWidgetDialog(onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = AetherColors.BgCard,
        titleContentColor = AetherColors.TextMain,
        textContentColor = AetherColors.TextMuted,
        title = {
            Text(
                text = "Встроенный виджет OriginOS 6",
                fontWeight = FontWeight.Bold,
                fontSize = 17.sp,
            )
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (BuildConfig.IS_ORIGIN_OS_WIDGET_BUILD) {
                    Text(
                        text = "Эта сборка использует системный идентификатор Spotify (com.spotify.music) + MediaLibraryService / MediaBrowserService, чтобы встроенный Атомный плеер OriginOS 6 распознавал Aether Music нативно.",
                        fontSize = 13.sp,
                    )
                    Text(
                        text = "1. Удалите официальный Spotify (если установлен).\n2. Разрешите уведомления и фоновую работу без ограничений батареи для Aether Music.\n3. На виджете «Атомный плеер» нажмите кнопку переключения источника и выберите Spotify — подключится Aether Music.",
                        fontSize = 13.sp,
                        color = AetherColors.TextMain,
                    )
                } else {
                    Text(
                        text = "Установлена стандартная сборка (com.aether.music). Она работает в Origin Island, на экране блокировки и в шторке, и не конфликтует со Spotify.\n\nЧтобы плеер появился внутри встроенного Атомного плеера OriginOS 6 на рабочем столе, установите APK-сборку AetherMusic-OriginOS-Widget.apk.",
                        fontSize = 13.sp,
                    )
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) {
                Text("Понятно", color = AetherColors.WaveGold)
            }
        },
    )
}
