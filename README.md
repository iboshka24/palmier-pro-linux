# Palmier Pro Linux — улучшенный Linux-форк 0.4.0

Нативный Qt 6 видеоредактор для Linux, развивающий
[compotik2/palmier-pro-windows](https://github.com/compotik2/palmier-pro-windows)
с портированием функций из [palmier-io/palmier-pro](https://github.com/palmier-io/palmier-pro)
(`last-gpl-source`, v0.7.6, GPLv3) и переработанным дизайном.

Исходник: [`native/`](native/README.md) — Python + PySide6 + FFmpeg, MCP на
`http://127.0.0.1:19789/mcp`. Это неполный GPL-порт, а не 1:1 копия macOS-версии.

## Что нового в 0.4.0-linux

**Больше функций из оригинала (53 MCP-инструмента вместо 20):**
- `inspect_timeline` — богатое чтение: гэпы, маркеры, transform/crop/color/effects, ссылки
- `manage_markers` — маркеры-рецензии (создание/правка/удаление)
- `inspect_media`, `search_media`, `organize_media` — папки и поиск по библиотеке
- `insert_clips` (ripple-вставка со сдвигом sync-locked дорожек), `ripple_delete_ranges`
- `swap_clip_media`, `copy_clip_settings`, `manage_clip_links`, `sync_clips`
- `set_keyframes` (хранение), `apply_layout` — full / side-by-side / top-bottom / 4×PiP / grid 2×2 / sidebar / three-up / three-stack
- `add_captions` (группы субтитров), `capture_frame` (PNG кадра)
- `apply_color` (exposure/contrast/saturation/temperature/highlights/shadows — рендерится через FFmpeg `eq`/`colorbalance`), `inspect_color`
- `apply_effect` (grayscale/blur/sharpen/vignette/mirror/denoise-light — реально рендерится), `denoise_audio` (`afftdn`)
- `remove_silence` (по `silencedetect` + ripple), `manage_multicam` / `change_cam` / `get_multicam`
- `list_models` (честные локальные фильтры + найденные HW-энкодеры), генерация — честный `canGenerate:false`
- `linux_playback` / `linux_redo` (алиасы к `windows_*` для совместимости)
- Трансформы и кроп теперь **рендерятся** (слот + overlay-позиция, rotate, flip), а не отвергаются

**Дизайн:**
- Тёмная Linux-тема: фон `#0F1115`, панели `#14171D`, акцент `#4C7DFF`, скругления 8–12px
- Таймлайн: дорожка маркеров, бейджи `● fx 🔗 🎧`, fade-треугольники, ruler `мм:сс`, красный плейхед с флажком
- Инспектор: Clip + Color (exposure/contrast/saturation/temperature) + FX + Denoise + текст
- Тулбар: Marker, CC, Layout; превью с закруглёнными углами; моноширинный таймкод

**Linux:**
- X11 и XWayland, PipeWire/Pulse через Qt Multimedia, VA-API/NVENC детект с fallback
- `.desktop` файл, `PALMIER_FONT_PATH`, понятные ошибки про FFmpeg/шрифты
- PySide6 без жёсткого пина (6.8+), FFmpeg 4.4+ / 7.x (обе ветки `filter_complex`)

## Запуск (Ubuntu 22.04+)

```bash
sudo apt-get update
sudo apt-get install ffmpeg fonts-liberation fonts-dejavu-core \
  libegl1 libopengl0 libgl1 libxcb-cursor0 libxkbcommon-x11-0
git clone https://github.com/iboshka24/palmier-pro-linux.git
cd palmier-pro-linux/native
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

Подробно: [`native/LINUX.md`](native/LINUX.md).

## MCP / AI

Откройте редактор, затем в OpenCode:

```json
"palmier-pro": { "type": "remote", "url": "http://127.0.0.1:19789/mcp", "enabled": true }
```

```bash
codex mcp add palmier-pro --url http://127.0.0.1:19789/mcp
```

Loopback-only, строгий Host, запрет browser Origin, лимит 1 МиБ.
Опционально `PALMIER_MCP_TOKEN` → `Authorization: Bearer TOKEN`.

## Совместимость

| Возможность | Состояние 0.4.0 |
|---|---|
| Мультитрек V/A, move/split/trim/undo | ✅ + тесты |
| Маркеры, ripple insert/delete, swap, copy settings, links, sync | ✅ |
| Трансформ/кроп, цвет, эффекты, denoise | ✅ рендер через FFmpeg |
| Раскладки PiP/split/grid, субтитры, keyframes (хранение) | ✅ |
| Captions из оригинала, bead/speech-модели, Lottie, keyframe-анимация в рендере | ❌ честный отказ |
| Генерация (Seedance/Kling и т.д.), подписка Palmier | ❌ `canGenerate:false`, бэкенда нет в GPL |
| Linked AV как в оригинале, ripple-мультикам, nested timelines | ⚠️ частично (linkGroupId, multicam-группы базовые) |

Подробнее: [`native/README.md`](native/README.md), [`native/docs/MIGRATION.md`](native/docs/MIGRATION.md).

## Проверка

```bash
cd native
python3 -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python3 -c "from palmier.ui import Window; print('UI OK')"
```

## Лицензия

GPLv3. Оригинал: Palmier, Inc. (`last-gpl-source`, коммит `8805801`).
Windows-порт: compotik2. Linux-форк: iboshka24. См. LICENSE, NOTICE,
`native/docs/THIRD_PARTY.md`.
