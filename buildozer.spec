[app]
title = Rupor Events
package.name = ruporevents
package.domain = org.phaseshift
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,txt,md
version = 0.1
requirements = python3,kivy,requests,beautifulsoup4,certifi,urllib3,idna,charset-normalizer
orientation = portrait
fullscreen = 0

# Android permissions
android.permissions = INTERNET

# Fixed Android toolchain versions for GitHub Actions
android.api = 35
android.minapi = 23
android.build_tools = 35.0.0
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a

# Use SDK/NDK installed by GitHub Actions instead of Buildozer-downloaded empty SDK
android.sdk_path = /usr/local/lib/android/sdk
android.ndk_path = /usr/local/lib/android/sdk/ndk/25.2.9519653

android.allow_backup = True
android.enable_androidx = True

# Keep default python-for-android branch unless you need experimental fixes
# p4a.branch = master

log_level = 2
warn_on_root = 1

[buildozer]
log_level = 2
warn_on_root = 1
