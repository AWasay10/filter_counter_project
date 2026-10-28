"""
server.py  -  runs count_filters.py behind a tiny web API so the phone app can use it.

  pip install -r requirements.txt
  python server.py

The phone and this PC must be on the same Wi-Fi (or the PC on the phone's hotspot).
The address to type into the app is printed when the server starts.
"""
import base64, socket, threading, time, traceback
import cv2, numpy as np
from flask import Flask, Response, request, jsonify
import count_filters as cf

PORT = 5000
MAX_OUT_PX = 3200            # the annotated picture sent back to the phone is scaled to this (longest side)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024
LOCK = threading.Lock()      # one photo at a time (the analysis uses a lot of CPU)


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#00796b">
<title>Filter Counter</title>
<style>
  :root { --bg:#fafafa; --fg:#1b1b1b; --card:#fff; --brand:#00796b; --line:#d9d9d9; }
  @media (prefers-color-scheme: dark) { :root { --bg:#121212; --fg:#eee; --card:#1e1e1e; --line:#333; } }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { background:var(--brand); color:#fff; padding:14px 16px; font-size:20px; font-weight:600; }
  main { max-width:760px; margin:0 auto; padding:16px; }
  p.tip { font-size:14px; opacity:.8; }
  .btn { display:block; text-align:center; padding:16px; margin:10px 0; border-radius:12px; font-size:18px;
         background:var(--brand); color:#fff; cursor:pointer; }
  .btn.alt { background:transparent; color:var(--brand); border:2px solid var(--brand); }
  #status { margin:14px 0; min-height:22px; }
  #status.err { color:#c62828; font-weight:600; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; margin-top:12px; }
  .count { font-size:52px; font-weight:700; line-height:1.1; }
  .count small { font-size:16px; font-weight:400; }
  #stats { font-size:14px; opacity:.8; margin-top:4px; }
  #warn div { margin-top:8px; font-size:14px; color:#e65100; }
  #img { width:100%; margin-top:12px; border-radius:8px; background:#000; }
  a { color:var(--brand); }
</style></head>
<body>
<header>Filter Counter</header>
<main>
  <p class="tip">Take the photo from straight above with the whole tube area in the frame and no glare.
  Tap the tubes on the camera screen first so the exposure is set on them.</p>
  <label class="btn">Take a photo
    <input id="cam" type="file" accept="image/*" capture="environment" hidden></label>
  <label class="btn alt">Choose from gallery
    <input id="gal" type="file" accept="image/*" hidden></label>
  <div id="status"></div>
  <div id="out" class="card" hidden>
    <div class="count"><span id="n"></span> <small>filters counted</small></div>
    <div id="stats"></div>
    <div id="warn"></div>
    <img id="img" alt="counted filters">
    <p><a id="full" target="_blank">Open the picture full size (pinch to zoom)</a></p>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
async function run(file) {
  if (!file) return;
  $('out').hidden = true;
  $('status').className = '';
  $('status').textContent = 'Uploading and counting... (5 to 60 seconds)';
  const fd = new FormData();
  fd.append('image', file, file.name || 'photo.jpg');
  try {
    const r = await fetch('/count', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    $('n').textContent = j.count;
    $('stats').textContent = j.rows + ' rows, ' + j.per_row_min + '-' + j.per_row_max + ' per row | ' + j.seconds + ' s';
    $('warn').innerHTML = '';
    (j.warnings || []).forEach(w => { const d = document.createElement('div'); d.textContent = 'Warning: ' + w; $('warn').appendChild(d); });
    const bin = atob(j.image), arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([arr], { type: 'image/jpeg' }));
    $('img').src = url; $('full').href = url;
    $('out').hidden = false; $('status').textContent = '';
  } catch (e) {
    $('status').className = 'err';
    $('status').textContent = 'Error: ' + e.message;
  }
}
for (const id of ['cam', 'gal']) {
  $(id).addEventListener('change', e => { run(e.target.files[0]); e.target.value = ''; });
}
</script>
</body></html>"""


def _num(value, default=None):
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def process(data, pitch=None, box=None, diameter=cf.FILTER_DIAMETER_MM):
    """photo bytes -> dict with the count, some statistics and the annotated picture (base64 jpg)"""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("The file is not a readable image.")
    t0 = time.time()
    res = cf.analyse(img, pitch)                       # may raise RuntimeError with a readable message
    xy, ij = res["xy"], res["ij"]
    n = len(xy)
    rows = np.unique(ij[:, 1])
    per_row = [int((ij[:, 1] == j).sum()) for j in rows]

    warnings, cap = [], None
    if res["pitch"] < 6:
        warnings.append("Tubes are less than 6 px apart in the photo - too low resolution for a reliable count. Move closer.")
    elif res["pitch"] < 10:
        warnings.append("Tubes are small in the photo (under 10 px apart). Move closer for a more reliable count.")
    nb = len(res["boxfilled"])
    if nb:
        warnings.append(f"{nb} tubes were over-exposed / had no visible detail and were counted from their grid position. "
                        f"A slightly darker photo is more reliable.")
    if box:
        lb, sb = max(box), min(box)
        cap = lb * sb / (diameter ** 2 * np.sqrt(3) / 2)
        if n > cap:
            warnings.append(f"Count {n} is above the physical maximum {cap:.0f} for this box - circles are probably outside the box.")

    out = cf.render_result(img, xy, cf.numbering(ij, xy), res["pitch"])
    s = min(1.0, MAX_OUT_PX / max(out.shape[:2]))
    if s < 1.0:
        out = cv2.resize(out, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise RuntimeError("could not encode the result picture")
    return dict(
        count=n, rows=len(rows), per_row_min=min(per_row), per_row_max=max(per_row),
        pitch=round(float(res["pitch"]), 2), boxfilled=nb,
        capacity=None if cap is None else round(float(cap), 1),
        warnings=warnings, seconds=round(time.time() - t0, 1),
        image=base64.b64encode(buf.tobytes()).decode("ascii"),
    )


@app.get("/")
def home():
    """phone-friendly web page: open http://<pc address>:5000 in the phone's browser"""
    return Response(PAGE, mimetype="text/html")


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.post("/count")
def count():
    f = request.files.get("image")
    if f is None:
        return jsonify(error="No photo was received."), 400
    pitch = _num(request.form.get("pitch"))
    bw, bh = _num(request.form.get("box_w")), _num(request.form.get("box_h"))
    box = (bw, bh) if bw and bh else None
    diameter = _num(request.form.get("diameter"), cf.FILTER_DIAMETER_MM)
    data = f.read()
    print(f"[count] photo {len(data) / 1e6:.1f} MB")
    try:
        with LOCK:
            out = process(data, pitch, box, diameter)
        print(f"[count] result {out['count']} in {out['seconds']} s")
        return jsonify(out)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except RuntimeError as e:                          # analysis could not find a tube block
        print("[count] analysis failed:", e)
        return jsonify(error=f"Could not count this photo: {e}"), 422
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"Server error: {e}"), 500


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1)); ips.add(s.getsockname()[0]); s.close()
    except Exception:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


if __name__ == "__main__":
    print("\nFilter counter server is running.")
    print("Type ONE of these into the phone app (Settings -> Server address):")
    for ip in local_ips():
        print(f"    http://{ip}:{PORT}")
    print("QUICK DEMO: open that same address in the phone's browser - it is a ready-to-use counting page.")
    print("Check the connection first:  <address>/health  ->  {\"ok\":true}\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True)