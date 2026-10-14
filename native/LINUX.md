# Palmier Pro Linux 0.4.0 — запуск и сборка

Нативный Qt 6 Widgets редактор: модель проекта, таймлайн, FFmpeg рендер/экспорт
и локальный MCP-сервер. Неполный GPL-порт Palmier, не pixel-perfect.
См. корневой README (таблица совместимости), LICENSE/NOTICE.

## Поддерживаемый профиль

- Linux **x86_64**, Ubuntu 22.04+ (glibc 2.35). Другие дистрибутивы — при совместимых
  glibc, графических/аудио библиотеках и FFmpeg. ARM не пакуется.
- X11 или XWayland (`echo $XDG_SESSION_TYPE`). Чистый Wayland без XWayland не сертифицирован.
- FFmpeg/FFprobe **4.4+** с drawtext, libx264, libx265. Проверены ветки 4.x–9.x.
- Шрифт Liberation Sans / DejaVu Sans. `PALMIER_FONT_PATH=/путь/к.ttf` — свой шрифт.
- Аудио: PipeWire/Pulse через Qt Multimedia. Видео: Qt Multimedia + CPU-презентация;
  NVENC/VA-API пробуются при экспорте с fallback на софт.

Портативный архив включает Python, PySide6/Qt и декодерные библиотеки.
FFmpeg/FFprobe — внешние, ставятся отдельно.

## Установка и запуск (Ubuntu)

```bash
sudo apt-get update
sudo apt-get install ffmpeg fonts-liberation fonts-dejavu-core \
  libegl1 libopengl0 libgl1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0
tar -xzf PalmierProLinux-0.4.0-linux-x86_64-portable.tar.gz
cd PalmierProLinux
./PalmierProLinux
```

Не разносите бинарник и `_internal`. Для `.desktop`:

```bash
mkdir -p ~/.local/bin ~/.local/share/applications
ln -sf "$PWD/PalmierProLinux" ~/.local/bin/
cp palmier-pro-linux.desktop ~/.local/share/applications/
```

Горячие клавиши — Ctrl. Проекты — каталоги `.palmier`. Импорты хранят абсолютные
POSIX-пути; Windows пути с буквой диска нужно перелинковать вручную.

При проблемах:

```bash
./PalmierProLinux --verify-media --output /tmp/media.json
ffmpeg -version && ffprobe -version
echo $XDG_SESSION_TYPE
```

## OpenCode

Держите редактор открытым, в `~/.config/opencode/opencode.jsonc`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "palmier-pro": {
      "type": "remote",
      "url": "http://127.0.0.1:19789/mcp",
      "enabled": true
    }
  }
}
```

Перезапустите OpenCode, проверьте `opencode mcp list`.
53 инструмента: маркеры, ripple, раскладки, субтитры, цвет, эффекты, denoise,
ссылки, кейфреймы, `capture_frame`. Генерация честно отвечает `canGenerate:false`.

Loopback-only, строгий Host, отказ browser Origin. `PALMIER_MCP_TOKEN` включает
Bearer-авторизацию.

## Сборка из исходников

Нужны Python 3.11+ и зависимости выше:

```bash
git clone https://github.com/iboshka24/palmier-pro-linux.git
cd palmier-pro-linux/native
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/build.py
./dist/PalmierProLinux/PalmierProLinux --verify-media --output test-output/media.json
```

`bash build.sh` — тесты + упаковка + проверки в графической сессии.
Node.js 24 опционален для `mcp-client` проверки.

GitHub Actions: Ubuntu 22.04 + Xvfb + null-аудио — окно, мышь/клавиатура,
видимый плейбэк, source/media тесты. Живой звук, Wayland и все дистрибутивы
автоматически не покрываются.
