"""Run this inside the generated Flutter project folder (the one that contains the android/ folder).
It lets the app use the network (INTERNET permission) and talk to your PC over plain http://."""
import pathlib, sys

p = pathlib.Path("android/app/src/main/AndroidManifest.xml")
if not p.exists():
    sys.exit("android/app/src/main/AndroidManifest.xml not found - run this from the Flutter project folder.")
t = p.read_text(encoding="utf-8")
if "android.permission.INTERNET" not in t:
    t = t.replace("<application", '<uses-permission android:name="android.permission.INTERNET"/>\n    <application', 1)
if "usesCleartextTraffic" not in t:
    t = t.replace("<application", '<application\n        android:usesCleartextTraffic="true"', 1)
p.write_text(t, encoding="utf-8")
print("AndroidManifest.xml patched: INTERNET permission + http allowed")
