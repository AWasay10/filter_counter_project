# Filter Counter  (phone app + PC server)

The phone app takes a photo and sends it to a small server on your PC.
The server runs `count_filters.py` (the same code you already tested) and sends back the count and the
photo with every tube circled and numbered. You can pinch-zoom the result on the phone.

    phone app  --photo-->  server.py (your PC, count_filters.py)  --count + circled photo-->  phone app

## 1. Start the server on the PC (Windows)

    cd server
    pip install -r requirements.txt
    python server.py          (or double-click start_server.bat)

It prints addresses like `http://192.168.1.23:5000`. Windows may ask to allow Python through the firewall:
tick **Private networks**. Check from the phone's browser: `http://192.168.1.23:5000/health` must show `{"ok":true}`.
The phone and PC must be on the same Wi-Fi (or put the PC on the phone's hotspot).

## 2. Get the APK  (no Android Studio needed)

### Option A - build it in the cloud with GitHub (easiest)
1. Make a new empty repository on github.com and upload the **contents of this folder**
   (use GitHub Desktop, or `git push`; the hidden `.github` folder must be included).
2. Open the repository -> **Actions** tab -> **Build Android APK** -> **Run workflow**.
3. Wait about 6-10 minutes. Open the finished run -> **Artifacts** -> download `filter-counter-apk`.
4. Unzip it, send `app-release.apk` to the phone, tap it, allow "install unknown apps".

### Option B - build on your PC (needs Flutter + Android Studio installed)

    flutter create --platforms=android --project-name filter_counter --org com.example build_app
    copy app\pubspec.yaml build_app\pubspec.yaml
    copy app\lib\main.dart build_app\lib\main.dart
    cd build_app
    python ..\app\patch_manifest.py
    flutter pub get
    flutter build apk --release

The APK is `build_app\build\app\outputs\flutter-apk\app-release.apk`.

## 3. Use it
1. Open the app -> Settings (gear) -> type the server address -> **Test connection** -> Save.
2. **Take a photo** (or choose one from the gallery). Wait while it counts (about 5-60 s).
3. The result screen shows the circled photo and the count.

Optional in Settings: box width / height and filter diameter (mm). The app then warns if the count is
higher than the box can physically hold.

## Tuning
All thresholds are at the top of `server/count_filters.py` (TEX_REL, LUM_MIN, HULL_MARGIN, SIDE_FRAC ...).
Restart `server.py` after changing them.

## Photo tips
Straight from above, whole tube area in the frame, no flash, tap the tubes on the screen to set exposure
(a slightly darker photo is better than a washed-out one), tubes at least ~10 px apart in the photo.
