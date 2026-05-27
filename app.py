import os
import sys
import json
import subprocess
import threading
import uuid
import re
import time
import platform
import webbrowser
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

IS_WINDOWS = platform.system() == 'Windows'

# On Windows, hide the console windows of every subprocess (ffmpeg, ffprobe,
# vulkaninfo, etc). Without this, every probe flashes a black CMD window.
_SUBPROCESS_KWARGS = {}
if IS_WINDOWS:
    _SUBPROCESS_KWARGS['creationflags'] = 0x08000000  # CREATE_NO_WINDOW


# ─── PyInstaller resource paths ───────────────────────────────────────────────
def resource_path(relative):
    """Absolute path to a bundled resource. Works in dev and PyInstaller onefile."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), relative)


def app_data_dir():
    """Persistent dir for config/temp — next to the executable, NOT in _MEIPASS."""
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    d = base / 'forge-data'
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundled_binary(name):
    """Return path to a bundled binary if present, else empty string.
    Tries name and name.exe (Windows)."""
    candidates = [name]
    if IS_WINDOWS and not name.endswith('.exe'):
        candidates.append(name + '.exe')

    bases = []
    if hasattr(sys, '_MEIPASS'):
        bases.append(Path(sys._MEIPASS) / 'bin')
    bases.append(Path(__file__).parent / 'bin')

    for base in bases:
        for nm in candidates:
            p = base / nm
            if p.exists():
                return str(p)
    return ''


TEMPLATES_DIR = resource_path('templates')
app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config['SECRET_KEY'] = 'ffmpeg-encoder-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

CONFIG_FILE = app_data_dir() / 'config.json'

_BUNDLED_FFMPEG = bundled_binary('ffmpeg')
_BUNDLED_FFPROBE = bundled_binary('ffprobe')

_DEFAULT_SYSTEM_FFMPEG = 'ffmpeg.exe' if IS_WINDOWS else '/usr/bin/ffmpeg'
_DEFAULT_SYSTEM_FFPROBE = 'ffprobe.exe' if IS_WINDOWS else '/usr/bin/ffprobe'

DEFAULT_CONFIG = {
    'ffmpeg_path': _BUNDLED_FFMPEG or _DEFAULT_SYSTEM_FFMPEG,
    'ffprobe_path': _BUNDLED_FFPROBE or _DEFAULT_SYSTEM_FFPROBE,
    'temp_path': str(app_data_dir() / 'temp'),
    'parallel_jobs': False,
    'max_workers': 1,
}

encode_jobs = {}
job_lock = threading.Lock()
encode_queue = []
queue_running = False
queue_thread = None


def _is_stale_meipass(path):
    """Detect a saved ffmpeg path pointing at a previous PyInstaller unpack dir."""
    if not path:
        return False
    return ('/_MEI' in path or '\\_MEI' in path) and not os.path.exists(path)


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except Exception:
            pass

    if _is_stale_meipass(cfg.get('ffmpeg_path')) and _BUNDLED_FFMPEG:
        cfg['ffmpeg_path'] = _BUNDLED_FFMPEG
    if _is_stale_meipass(cfg.get('ffprobe_path')) and _BUNDLED_FFPROBE:
        cfg['ffprobe_path'] = _BUNDLED_FFPROBE

    return cfg


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


# ─── GPU / encoder detection ─────────────────────────────────────────────────
# On Linux we use vainfo + vulkaninfo as before. On Windows we ask ffmpeg
# directly which encoders it has and offer all the relevant ones; the user
# picks. Trying to enumerate D3D11 adapters from Python is more trouble than
# it's worth.

def _get_gpus_linux():
    gpus = [{'id': 'cpu', 'name': 'CPU (Software)',
             'encoders': ['libx264', 'libx265', 'libsvtav1']}]

    try:
        result = subprocess.run(['vainfo', '--display', 'drm'],
                                capture_output=True, text=True, timeout=5,
                                **_SUBPROCESS_KWARGS)
        output = result.stdout + result.stderr
        if 'VAEntrypointEncSlice' in output or 'VAEntrypointEncSliceLP' in output:
            name_match = re.search(r'Driver version.*?:\s*(.+)', output)
            va_name = name_match.group(1).strip() if name_match else 'Intel/AMD GPU (VAAPI/QSV)'
            encoders = []
            if 'H264' in output or 'AVC' in output:
                encoders += ['h264_qsv', 'h264_vaapi']
            if 'H265' in output or 'HEVC' in output:
                encoders += ['hevc_qsv', 'hevc_vaapi']
            if 'AV1' in output:
                encoders += ['av1_qsv', 'av1_vaapi']
            if encoders:
                gpus.append({'id': 'qsv', 'name': va_name, 'encoders': encoders})
    except Exception:
        pass

    try:
        result = subprocess.run(['vulkaninfo', '--summary'],
                                capture_output=True, text=True, timeout=5,
                                **_SUBPROCESS_KWARGS)
        if 'deviceName' in result.stdout:
            for line in result.stdout.splitlines():
                if 'deviceName' in line:
                    vk_name = line.split('=')[-1].strip()
                    gpus.append({'id': 'vulkan',
                                 'name': f'{vk_name} (Vulkan)',
                                 'encoders': ['hevc_vulkan', 'h264_vulkan']})
                    break
    except Exception:
        pass

    return gpus


def _get_gpus_windows():
    """Detect GPUs on Windows by:
       1) parsing WMIC / PowerShell for GPU names (informational)
       2) asking ffmpeg which encoders it actually supports."""
    gpus = [{'id': 'cpu', 'name': 'CPU (Software)',
             'encoders': ['libx264', 'libx265', 'libsvtav1']}]

    # Get installed video adapter names for informational labelling
    gpu_names = []
    try:
        ps = (
            "Get-CimInstance Win32_VideoController | "
            "Select-Object -ExpandProperty Name"
        )
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps],
            capture_output=True, text=True, timeout=8, **_SUBPROCESS_KWARGS
        )
        gpu_names = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except Exception:
        pass

    # Ask ffmpeg what hw encoders it has
    available = set()
    try:
        ffmpeg = load_config().get('ffmpeg_path', 'ffmpeg')
        result = subprocess.run([ffmpeg, '-hide_banner', '-encoders'],
                                capture_output=True, text=True, timeout=8,
                                **_SUBPROCESS_KWARGS)
        for line in result.stdout.splitlines():
            for enc in ('h264_nvenc', 'hevc_nvenc', 'av1_nvenc',
                        'h264_qsv',   'hevc_qsv',   'av1_qsv',
                        'h264_amf',   'hevc_amf',   'av1_amf',
                        'h264_vulkan','hevc_vulkan'):
                if enc in line:
                    available.add(enc)
    except Exception:
        pass

    def pick(prefix):
        return [e for e in available if e.endswith('_' + prefix)]

    # NVIDIA (NVENC)
    nvenc = pick('nvenc')
    if nvenc:
        nv_label = next((n for n in gpu_names if 'NVIDIA' in n.upper()),
                        'NVIDIA GPU')
        gpus.append({'id': 'nvenc', 'name': f'{nv_label} (NVENC)',
                     'encoders': sorted(nvenc)})

    # Intel (QSV)
    qsv = pick('qsv')
    if qsv:
        intel_label = next((n for n in gpu_names if 'INTEL' in n.upper()),
                           'Intel GPU')
        gpus.append({'id': 'qsv', 'name': f'{intel_label} (QSV)',
                     'encoders': sorted(qsv)})

    # AMD (AMF)
    amf = pick('amf')
    if amf:
        amd_label = next((n for n in gpu_names
                          if 'AMD' in n.upper() or 'RADEON' in n.upper()),
                         'AMD GPU')
        gpus.append({'id': 'amf', 'name': f'{amd_label} (AMF)',
                     'encoders': sorted(amf)})

    # Vulkan (vendor-agnostic, last)
    vulkan = pick('vulkan')
    if vulkan:
        vk_label = gpu_names[0] if gpu_names else 'GPU'
        gpus.append({'id': 'vulkan', 'name': f'{vk_label} (Vulkan)',
                     'encoders': sorted(vulkan)})

    return gpus


def get_gpus():
    return _get_gpus_windows() if IS_WINDOWS else _get_gpus_linux()


def probe_file(filepath, ffprobe_path):
    try:
        cmd = [ffprobe_path, '-v', 'quiet', '-print_format', 'json',
               '-show_streams', '-show_format', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30, **_SUBPROCESS_KWARGS)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)

        info = {
            'duration': float(data.get('format', {}).get('duration', 0)),
            'size': int(data.get('format', {}).get('size', 0)),
            'video': [], 'audio': [], 'subtitle': [], 'hdr': False,
        }

        for stream in data.get('streams', []):
            codec_type = stream.get('codec_type')
            if codec_type == 'video':
                color_transfer = stream.get('color_transfer', '')
                color_primaries = stream.get('color_primaries', '')
                is_hdr = any(x in (color_transfer + color_primaries)
                             for x in ['smpte2084', 'arib-std-b67', 'bt2020'])
                info['hdr'] = is_hdr
                info['video'].append({
                    'index': stream['index'],
                    'codec': stream.get('codec_name'),
                    'width': stream.get('width'),
                    'height': stream.get('height'),
                    'fps': stream.get('r_frame_rate', ''),
                    'color_space': stream.get('color_space', ''),
                    'color_transfer': color_transfer,
                    'hdr': is_hdr,
                })
            elif codec_type == 'audio':
                info['audio'].append({
                    'index': stream['index'],
                    'codec': stream.get('codec_name'),
                    'channels': stream.get('channels', 2),
                    'language': stream.get('tags', {}).get('language', 'und'),
                    'title': stream.get('tags', {}).get('title', ''),
                })
            elif codec_type == 'subtitle':
                info['subtitle'].append({
                    'index': stream['index'],
                    'codec': stream.get('codec_name'),
                    'language': stream.get('tags', {}).get('language', 'und'),
                    'title': stream.get('tags', {}).get('title', ''),
                })
        return info
    except Exception as e:
        return {'error': str(e)}


def build_ffmpeg_cmd(job, config):
    ffmpeg = config['ffmpeg_path']
    src = job['input_file']
    out = job['output_file']
    settings = job['settings']

    encoder = settings.get('video_encoder', 'libx264')

    # Coerce quality to int. The Quality field is a number in the UI but the
    # browser doesn't strictly enforce that, and old config blobs may carry
    # strings. Reject non-numeric input and fall back to a sensible default
    # rather than passing "high" / "" / None through to ffmpeg, which fails
    # cryptically as "Unable to parse 'qp' option value".
    raw_quality = settings.get('quality', 23)
    try:
        quality = int(str(raw_quality).strip())
    except (TypeError, ValueError):
        quality = 23
    # Clamp to a sane range — different encoders accept different ranges
    # (0-51 for x264/x265/nvenc, 0-63 for AV1, 0-100 for QSV global_quality),
    # but 0-51 is universally accepted; anything higher gets capped per-encoder.
    quality = max(0, min(quality, 63))

    preset = settings.get('preset', 'medium')
    hdr_mode = settings.get('hdr_mode', 'passthrough')
    is_hdr = job.get('probe', {}).get('hdr', False)

    src_pix_fmt = 'yuv420p'
    video_streams = job.get('probe', {}).get('video', [])
    if video_streams and '10' in str(video_streams[0]):
        src_pix_fmt = 'yuv420p10le'

    cmd = [ffmpeg, '-y']

    # Hardware accel init — platform-specific
    if 'vaapi' in encoder and not IS_WINDOWS:
        cmd += ['-hwaccel', 'vaapi',
                '-hwaccel_device', '/dev/dri/renderD128',
                '-hwaccel_output_format', 'vaapi']
    elif 'vulkan' in encoder:
        # Initialize the Vulkan device before -i so it's available to filters
        cmd += ['-init_hw_device', 'vulkan=vk:0']
    # NVENC and QSV/AMF on Windows don't need explicit hwaccel init for encode-only

    cmd += ['-i', src]
    cmd += ['-map', '0:v:0']
    cmd += ['-c:v', encoder]

    # quality_str is what we hand to ffmpeg. Keep it as a string for argv,
    # but ensure it's the numeric form, never an English word.
    q = str(quality)

    if encoder in ('libx264', 'libx265'):
        cmd += ['-crf', q, '-preset', preset]
    elif encoder == 'libsvtav1':
        cmd += ['-crf', q, '-preset', '6']
    elif 'nvenc' in encoder:
        # nvenc uses -cq for constant quality mode; -preset is p1..p7 (slowest→best)
        cmd += ['-rc', 'vbr', '-cq', q, '-preset', 'p5', '-tune', 'hq']
    elif 'qsv' in encoder:
        cmd += ['-global_quality', q]
        if not IS_WINDOWS:
            pix = 'p010le' if src_pix_fmt == 'yuv420p10le' else 'nv12'
            cmd += ['-vf', f'format={pix}']
    elif 'amf' in encoder:
        # AMF constant-QP. -qp_i / -qp_p are required; HEVC also takes -qp_b.
        # None of these accept words — must be 0..51 integers.
        cmd += ['-rc', 'cqp', '-qp_i', q, '-qp_p', q]
        if 'hevc' in encoder or 'av1' in encoder:
            cmd += ['-qp_b', q]
        # AMF's -quality preset is one of: speed, balanced, quality
        cmd += ['-quality', 'balanced']
    elif 'vaapi' in encoder:
        cmd += ['-global_quality', q]
        pix = 'p010le' if src_pix_fmt == 'yuv420p10le' else 'nv12'
        cmd += ['-vf', f'format={pix},hwupload']
    elif 'vulkan' in encoder:
        # Vulkan encode on the BtbN Windows build:
        #   - decode happens on CPU (software), output is in system memory
        #   - hwupload moves frames onto the Vulkan device
        #   - -qp:v sets the constant quantizer (NOT -qp, which is ambiguous
        #     and can collide with codec-private options on Windows)
        # 10-bit support through Vulkan is spotty across drivers; force 8-bit
        # NV12 by default to maximize compatibility.
        pix = 'nv12'
        cmd += ['-vf', f'format={pix},hwupload']
        cmd += ['-qp:v', q]

    if (is_hdr and hdr_mode == 'passthrough'
            and 'vaapi' not in encoder and 'vulkan' not in encoder):
        cmd += ['-colorspace', 'bt2020nc', '-color_trc', 'smpte2084', '-color_primaries', 'bt2020']
    elif hdr_mode == 'tonemapping' and encoder in ('libx264', 'libx265', 'libsvtav1'):
        cmd += ['-vf', 'zscale=t=linear,tonemap=hable,zscale=t=bt709,format=yuv420p']

    audio_streams = settings.get('audio_streams', [])
    if audio_streams:
        for idx in audio_streams:
            cmd += ['-map', f'0:{idx}']
    else:
        cmd += ['-map', '0:a?']

    audio_codec = settings.get('audio_codec', 'copy')
    cmd += ['-c:a', audio_codec]
    if audio_codec != 'copy':
        cmd += ['-b:a', settings.get('audio_bitrate', '192k')]

    sub_streams = settings.get('subtitle_streams', [])
    if sub_streams:
        for idx in sub_streams:
            cmd += ['-map', f'0:{idx}']
        cmd += ['-c:s', 'copy']
    else:
        cmd += ['-sn']

    temp_dir = config.get('temp_path', str(app_data_dir() / 'temp'))
    os.makedirs(temp_dir, exist_ok=True)
    ext = Path(out).suffix
    temp_out = str(Path(temp_dir) / f"encode_{job['id']}{ext}")

    cmd.append(temp_out)
    return cmd, temp_out


def run_encode_job(job_id):
    global queue_running
    config = load_config()

    with job_lock:
        job = encode_jobs.get(job_id)
        if not job:
            return
        job['status'] = 'encoding'
        job['progress'] = 0
        job['log'] = []

    socketio.emit('job_update', {'id': job_id, 'status': 'encoding', 'progress': 0})

    try:
        cmd, temp_out = build_ffmpeg_cmd(job, config)
        job['cmd'] = ' '.join(cmd)
        duration = job.get('probe', {}).get('duration', 0)

        process = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE,
            universal_newlines=True, bufsize=1, **_SUBPROCESS_KWARGS
        )

        with job_lock:
            job['pid'] = process.pid

        fps_pattern = re.compile(r'fps=\s*([\d.]+)')
        speed_pattern = re.compile(r'speed=\s*([\d.]+)x')
        time_pattern = re.compile(r'time=(\d+):(\d+):([\d.]+)')
        size_pattern = re.compile(r'size=\s*(\d+)kB')

        for line in process.stderr:
            line = line.strip()
            if not line:
                continue

            with job_lock:
                job['log'].append(line)
                if len(job['log']) > 200:
                    job['log'] = job['log'][-200:]

            update = {'id': job_id, 'log': line}

            time_match = time_pattern.search(line)
            if time_match and duration > 0:
                h, m, s = time_match.groups()
                current = int(h) * 3600 + int(m) * 60 + float(s)
                progress = min(int((current / duration) * 100), 99)
                with job_lock:
                    job['progress'] = progress
                update['progress'] = progress

            fps_m = fps_pattern.search(line)
            if fps_m:
                update['fps'] = fps_m.group(1)
            speed_m = speed_pattern.search(line)
            if speed_m:
                update['speed'] = speed_m.group(1) + 'x'
            size_m = size_pattern.search(line)
            if size_m:
                update['size'] = size_m.group(1) + ' kB'

            socketio.emit('job_update', update)

        process.wait()

        if process.returncode == 0:
            out_path = job['output_file']
            os.makedirs(Path(out_path).parent, exist_ok=True)
            # On Windows os.rename across drives fails; use replace which also
            # handles overwriting an existing destination.
            os.replace(temp_out, out_path)
            with job_lock:
                job['status'] = 'done'
                job['progress'] = 100
            socketio.emit('job_update', {'id': job_id, 'status': 'done', 'progress': 100})
        else:
            with job_lock:
                job['status'] = 'error'
                job['error'] = f'ffmpeg exited with code {process.returncode}'
            socketio.emit('job_update', {'id': job_id, 'status': 'error', 'error': job['error']})

    except Exception as e:
        with job_lock:
            if job_id in encode_jobs:
                encode_jobs[job_id]['status'] = 'error'
                encode_jobs[job_id]['error'] = str(e)
        socketio.emit('job_update', {'id': job_id, 'status': 'error', 'error': str(e)})


def queue_worker():
    global queue_running, encode_queue
    queue_running = True
    config = load_config()

    while True:
        with job_lock:
            pending = [jid for jid in encode_queue
                       if encode_jobs.get(jid, {}).get('status') == 'queued']
            if not pending:
                queue_running = False
                break
            if config.get('parallel_jobs'):
                max_w = config.get('max_workers', 1)
                active = sum(1 for jid in encode_jobs
                             if encode_jobs[jid].get('status') == 'encoding')
                if active >= max_w:
                    time.sleep(1)
                    continue
            job_id = pending[0]

        run_encode_job(job_id)
        if not config.get('parallel_jobs'):
            time.sleep(0.5)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(load_config())


@app.route('/api/config', methods=['POST'])
def update_config():
    cfg = load_config()
    cfg.update(request.json)
    # Don't persist _MEI temp paths — they're per-run.
    for key in ('ffmpeg_path', 'ffprobe_path'):
        v = cfg.get(key, '')
        if '/_MEI' in v or '\\_MEI' in v:
            cfg.pop(key, None)
    save_config(cfg)
    return jsonify({'ok': True})


@app.route('/api/gpus')
def api_gpus():
    return jsonify(get_gpus())


@app.route('/api/browse')
def browse():
    # On Windows, '/' isn't useful; default to user's home. Path() handles both.
    path = request.args.get('path', str(Path.home()))
    try:
        p = Path(path)
        if not p.exists():
            p = Path.home()
        entries = []
        if p.parent != p:
            entries.append({'name': '..', 'path': str(p.parent), 'type': 'dir'})
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            # On Windows, skip system/hidden via dotfile only — proper hidden
            # attr check would need ctypes; not worth it for a browser
            if item.name.startswith('.'):
                continue
            entry = {
                'name': item.name,
                'path': str(item),
                'type': 'dir' if item.is_dir() else 'file',
            }
            if item.is_file():
                ext = item.suffix.lower()
                entry['ext'] = ext
                if ext in ('.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts', '.wmv', '.flv', '.webm'):
                    entry['media'] = True
                    try:
                        entry['size'] = item.stat().st_size
                    except OSError:
                        entry['size'] = 0
            entries.append(entry)
        return jsonify({'path': str(p), 'entries': entries})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/probe', methods=['POST'])
def api_probe():
    filepath = request.json.get('file')
    config = load_config()
    info = probe_file(filepath, config.get('ffprobe_path', 'ffprobe'))
    if info is None:
        return jsonify({'error': 'probe failed'}), 400
    return jsonify(info)


@app.route('/api/queue', methods=['POST'])
def add_to_queue():
    global queue_running, queue_thread
    data = request.json
    job_id = str(uuid.uuid4())[:8]
    job = {
        'id': job_id,
        'input_file': data['input_file'],
        'output_file': data['output_file'],
        'settings': data['settings'],
        'probe': data.get('probe', {}),
        'status': 'queued',
        'progress': 0,
        'log': [],
        'added': time.time(),
    }
    with job_lock:
        encode_jobs[job_id] = job
        encode_queue.append(job_id)

    socketio.emit('job_added', {
        'id': job_id,
        'input_file': job['input_file'],
        'output_file': job['output_file'],
        'status': 'queued',
    })

    if not queue_running:
        queue_thread = threading.Thread(target=queue_worker, daemon=True)
        queue_thread.start()

    return jsonify({'job_id': job_id})


@app.route('/api/queue', methods=['GET'])
def get_queue():
    with job_lock:
        jobs = []
        for jid in encode_queue:
            job = encode_jobs.get(jid, {})
            jobs.append({
                'id': jid,
                'input_file': job.get('input_file', ''),
                'output_file': job.get('output_file', ''),
                'status': job.get('status', ''),
                'progress': job.get('progress', 0),
            })
    return jsonify(jobs)


@app.route('/api/job/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    with job_lock:
        job = encode_jobs.get(job_id)
        if job and job.get('pid'):
            try:
                if IS_WINDOWS:
                    # SIGTERM doesn't exist on Windows; use taskkill /T to kill
                    # the whole tree, since ffmpeg may spawn helpers.
                    subprocess.run(['taskkill', '/PID', str(job['pid']), '/T', '/F'],
                                   capture_output=True, **_SUBPROCESS_KWARGS)
                else:
                    import signal
                    os.kill(job['pid'], signal.SIGTERM)
            except Exception:
                pass
        if job:
            job['status'] = 'cancelled'
    socketio.emit('job_update', {'id': job_id, 'status': 'cancelled'})
    return jsonify({'ok': True})


@app.route('/api/job/<job_id>', methods=['GET'])
def get_job(job_id):
    job = encode_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify({k: v for k, v in job.items() if k != 'log'})


@app.route('/api/job/<job_id>/log', methods=['GET'])
def get_job_log(job_id):
    job = encode_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'log': job.get('log', [])})


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
    return jsonify({'ok': True})


def open_browser_when_ready(host, port):
    import socket
    url = f'http://{host}:{port}'
    for _ in range(60):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    host = '127.0.0.1'
    port = 5500

    # On Windows the exe is built with console=False, so prints go nowhere
    # useful. Skip the banner; the browser will tell the user everything.
    if not IS_WINDOWS:
        print('=' * 60)
        print('  FORGE — ffmpeg Encoder Workstation')
        print(f'  URL:        http://{host}:{port}')
        print(f'  Config:     {CONFIG_FILE}')
        print(f'  ffmpeg:     {DEFAULT_CONFIG["ffmpeg_path"]}'
              f' ({"BUNDLED" if _BUNDLED_FFMPEG else "system"})')
        print(f'  ffprobe:    {DEFAULT_CONFIG["ffprobe_path"]}'
              f' ({"BUNDLED" if _BUNDLED_FFPROBE else "system"})')
        print('  Press Ctrl+C to quit.')
        print('=' * 60)

    threading.Thread(target=open_browser_when_ready,
                     args=(host, port), daemon=True).start()

    socketio.run(app, host=host, port=port,
                 debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
