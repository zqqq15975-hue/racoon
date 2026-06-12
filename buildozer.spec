[app]
title = DHR Powerball AutoPick
package.name = dhrpowerballautopick
package.domain = org.gounsolution
source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,atlas,txt,json
version = 1.0.3
requirements = python3,kivy,requests,certifi,pyjnius
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 34
android.minapi = 23
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
p4a.bootstrap = sdl2

[buildozer]
log_level = 2
warn_on_root = 0
