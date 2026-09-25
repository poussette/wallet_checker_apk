[app]
title = Wallet Checker
package.name = walletchecker
package.domain = org.walletchecker
source.dir = .
source.include_exts = py
version = 0.1
requirements = python3,kivy==2.3.1,requests,certifi,urllib3,idna,charset_normalizer
orientation = portrait
fullscreen = 0

android.permissions = INTERNET
android.api = 34
android.minapi = 21
android.archs = arm64-v8a,armeabi-v7a
android.accept_sdk_license = True
# Avoids a slow/flaky first-time NDK download picking an unexpected version.
android.ndk = 25b

[buildozer]
log_level = 2
warn_on_root = 0
