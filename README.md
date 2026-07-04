# Rupor Events — Kivy + Buildozer

Android-приложение на Python/Kivy, собранное из парсера `rupor_parser.py`.

## Что умеет

- выбор города: `spb`, `msk`, `nsk`, `ekb`, `kzn`;
- дата: `сегодня`, `завтра`, `2026-07-04`, `4.07`, `4 июля`;
- фильтр по жанру;
- загрузка деталей события: время, адрес, описание;
- экспорт результата в TXT.

## Сборка APK на Linux / WSL

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv git zip unzip openjdk-17-jdk
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install buildozer cython
buildozer android debug
```

APK появится в папке:

```text
bin/ruporevents-0.1-arm64-v8a_armeabi-v7a-debug.apk
```

## Быстрый запуск на ПК без APK

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install kivy requests beautifulsoup4
python main.py
```

## Важные файлы

- `main.py` — Android/Kivy интерфейс;
- `rupor_parser.py` — логика парсинга, перенесена из исходного скрипта;
- `buildozer.spec` — конфиг сборки APK.

## Возможные проблемы

Если Buildozer ругается на SDK/NDK, запусти:

```bash
buildozer android clean
buildozer android debug -v
```

Первый билд может долго скачивать Android SDK, NDK и зависимости.
