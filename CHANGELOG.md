# Changelog

## 0.4.0-linux — Linux-форк

База: compotik2/palmier-pro-windows 0.3.0. Оригинал: palmier-io/palmier-pro `last-gpl-source` (8805801).

### Функции из оригинала
- `inspect_timeline` с gaps/markers/transform/color/effects/links
- `manage_markers`, `inspect_media`, `search_media`, `organize_media`
- `insert_clips` (ripple), `ripple_delete_ranges`, `swap_clip_media`, `copy_clip_settings`
- `manage_clip_links`, `sync_clips`, `set_keyframes`, `apply_layout` (11 раскладок)
- `add_captions`, `capture_frame`
- `apply_color` + `inspect_color`, `apply_effect` (6 фильтров), `denoise_audio`
- `remove_silence` (silencedetect), `manage_multicam`/`change_cam`/`get_multicam`
- `list_models`, честные `generate_*`/`upscale_media` (`canGenerate:false`)
- `linux_playback`/`linux_redo` алиасы
- Рендер трансформа/кропа/цвета/эффектов/denoise в FFmpeg (раньше — отказ)

### Дизайн
- Новая тёмная тема `#0F1115`/`#14171D`, акцент `#4C7DFF`
- Таймлайн: маркеры, бейджи, fade-треугольники, `мм:сс`, плейхед с флажком
- Инспектор: Color-knobs, FX, Denoise; тулбар Marker/CC/Layout

### Linux
- X11/XWayland, PipeWire/Pulse, VA-API/NVENC детект, гибкий PySide6>=6.8
- Обновлены README, LINUX.md, `.desktop`

### Тесты
- 40+ тестов: editor, mcp (53 инструмента), media (реальный FFmpeg), platforms
- Новый: цвет+трансформ+эффект рендер, маркеры/раскладки/ссылки через MCP
