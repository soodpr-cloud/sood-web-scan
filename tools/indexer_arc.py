#!/usr/bin/env python3
"""
สแกนรูปกิจกรรมโรงเรียนด้วย InsightFace (ArcFace) แล้วอัปเดตดัชนีใบหน้าใน Google Drive
ใช้คู่กับสมุดบันทึก Colab "face-scan-arcface.ipynb" (รูปแบบไฟล์ดัชนีและชื่อไฟล์เหมือนกันทุกอย่าง)

- สแกนเฉพาะรูปใหม่ และตัดรูปที่ถูกลบออกจากดัชนี รันซ้ำได้ปลอดภัย
- ไม่มีรูปใหม่ = ไม่โหลดโมเดล จบเร็ว
- ออกแบบให้รันบน GitHub Actions (CPU) ทุกคืน ถ้าเวลาไม่พอ จะหยุดเองแล้วทำต่อคืนถัดไป

ต้องมีตัวแปรสภาพแวดล้อม GOOGLE_APPLICATION_CREDENTIALS ชี้ไปที่ไฟล์ credentials ที่เข้าถึง Drive ได้
(บัญชีนั้นต้องแก้ไขโฟลเดอร์ดัชนี _faceindex_arc ได้ และอ่านโฟลเดอร์รูปได้)
"""
import argparse, base64, datetime, io, json, os, re, sys, time
import numpy as np
from PIL import Image, ImageOps
try:
    import pillow_heif; pillow_heif.register_heif_opener()
except Exception:
    pass

ENGINE = 'arcface-mbf-512'
FOLDER = 'application/vnd.google-apps.folder'
IMG_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif')
IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'}
TH_MONTHS = ['มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน', 'พฤษภาคม', 'มิถุนายน',
             'กรกฎาคม', 'สิงหาคม', 'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม']
TH_ABBR = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.']
EN_MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

CFG = dict(max_side=2400, det_size=1600, det_thresh=0.5, min_face=40)
_drive = None


# ---------------------------------------------------------------- Drive helpers
def drive():
    global _drive
    if _drive is None:
        import google.auth
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive'])
        _drive = build('drive', 'v3', credentials=creds, cache_discovery=False)
    return _drive


def api(make, tries=6):
    """เรียก Drive API ใหม่เมื่อการเชื่อมต่อหลุด (Broken pipe / หมดอายุระหว่างรันนาน ๆ)"""
    global _drive
    last = None
    for i in range(tries):
        try:
            return make(drive()).execute(num_retries=5)
        except Exception as e:  # noqa
            last = e
            print('  Drive error (%d/%d): %s' % (i + 1, tries, e))
            _drive = None
            time.sleep(2 * (i + 1))
    raise last


def esc(s):
    return s.replace('\\', '\\\\').replace("'", "\\'")


def ls(parent, extra=''):
    out, tok = [], None
    while True:
        r = api(lambda d: d.files().list(
            q="'%s' in parents and trashed=false %s" % (parent, extra),
            fields='nextPageToken, files(id,name,mimeType)', pageSize=1000, pageToken=tok,
            supportsAllDrives=True, includeItemsFromAllDrives=True))
        out += r['files']
        tok = r.get('nextPageToken')
        if not tok:
            return out


def name_of(fid):
    return api(lambda d: d.files().get(fileId=fid, fields='name', supportsAllDrives=True))['name']


def find_file(name, parent):
    r = api(lambda d: d.files().list(q="name='%s' and '%s' in parents and trashed=false" % (esc(name), parent),
                                     fields='files(id)', supportsAllDrives=True))['files']
    return r[0]['id'] if r else None


def get_bytes(fid):
    from googleapiclient.http import MediaIoBaseDownload
    last = None
    global _drive
    for i in range(4):
        try:
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, drive().files().get_media(fileId=fid, supportsAllDrives=True),
                                     chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=5)
            return buf.getvalue()
        except Exception as e:  # noqa
            last = e
            _drive = None
            time.sleep(2 * (i + 1))
    raise last


def load_json(name, parent):
    fid = find_file(name, parent)
    return json.loads(get_bytes(fid).decode('utf-8')) if fid else None


def save_json(name, obj, parent):
    from googleapiclient.http import MediaIoBaseUpload
    data = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    fid = find_file(name, parent)

    def make(d):
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype='application/json', resumable=True)
        if fid:
            return d.files().update(fileId=fid, media_body=media, supportsAllDrives=True)
        return d.files().create(body={'name': name, 'parents': [parent]}, media_body=media,
                                fields='id', supportsAllDrives=True)
    r = api(make)
    return fid or r['id']


# ---------------------------------------------------------------- folder parsing
def parse_year(name):
    m = re.search(r'(?<!\d)(\d{4})(?!\d)', name)
    if not m:
        return None
    y = int(m.group(1))
    if 1900 <= y < 2400:
        y += 543
    return y if 2400 <= y <= 2700 else None


def parse_month(name):
    for i, t in enumerate(TH_MONTHS):
        if t in name:
            return i + 1
    squeezed = name.replace(' ', '')
    for i, t in enumerate(TH_ABBR):
        if t in squeezed:
            return i + 1
    low = name.lower()
    for i, t in enumerate(EN_MONTHS):
        if re.search(r'(?<![a-z])' + t, low):
            return i + 1
    m = re.search(r'(?<!\d)(\d{1,2})(?!\d)', name)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1))
    return None


def parse_event(n):
    m = re.match(r'\s*\(\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{2,4})\s*\)\s*(.*)', n)
    if not m:
        return n.strip(), None
    d, mo, y, name = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).strip()
    if y < 100:
        y += 2500
    return (name or n.strip()), '%04d-%02d-%02d' % (y, mo, d)


def discover(roots):
    """รับ ID โฟลเดอร์ได้ทั้ง 'โฟลเดอร์ปี' (มีโฟลเดอร์เดือนข้างใน) หรือ 'โฟลเดอร์ราก' (มีโฟลเดอร์ปีข้างใน)"""
    events, skipped = [], []

    def add_year(year, yid):
        for m in ls(yid, "and mimeType='%s'" % FOLDER):
            if m['name'].startswith('_'):
                continue
            month = parse_month(m['name'])
            if month is None:
                skipped.append('โฟลเดอร์เดือนที่อ่านไม่ออก: %d/%s' % (year, m['name']))
                continue
            for e in ls(m['id'], "and mimeType='%s'" % FOLDER):
                if not e['name'].startswith('_'):
                    events.append({'year': year, 'month': month, 'folder': e['id'], 'raw': e['name']})

    for rid in roots:
        kids = [k for k in ls(rid, "and mimeType='%s'" % FOLDER) if not k['name'].startswith('_')]
        own = parse_year(name_of(rid))
        if own is not None and not any(parse_year(k['name']) for k in kids):
            add_year(own, rid)
            continue
        for y in kids:
            year = parse_year(y['name'])
            if year is None:
                skipped.append('โฟลเดอร์ปีที่อ่านไม่ออก: ' + y['name'])
                continue
            add_year(year, y['id'])
    return events, skipped


def images_in(folder_id):
    out, stack = [], [folder_id]
    while stack:
        for f in ls(stack.pop()):
            if f['mimeType'] == FOLDER:
                if not f['name'].startswith('_'):
                    stack.append(f['id'])
            elif f['mimeType'] in IMG_TYPES or os.path.splitext(f['name'])[1].lower() in IMG_EXT:
                out.append(f)
    out.sort(key=lambda p: (p['name'], p['id']))
    return out


# ---------------------------------------------------------------- face scanning
_models = None


def get_models():
    global _models
    if _models is None:
        from insightface.app import FaceAnalysis
        from insightface.model_zoo import model_zoo
        from insightface.utils.storage import ensure_available
        prov = ['CPUExecutionProvider']
        det = FaceAnalysis(name='buffalo_l', allowed_modules=['detection'], providers=prov)
        det.prepare(ctx_id=-1, det_thresh=CFG['det_thresh'], det_size=(CFG['det_size'], CFG['det_size']))
        small = ensure_available('models', 'buffalo_s', root='~/.insightface')
        rec = model_zoo.get_model(os.path.join(small, 'w600k_mbf.onnx'), providers=prov)
        rec.prepare(ctx_id=-1)
        _models = (det, rec)
        print('โหลดโมเดลแล้ว (mean/std = %s/%s)' % (rec.input_mean, rec.input_std))
    return _models


def to_bgr(raw):
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
    im.thumbnail((CFG['max_side'], CFG['max_side']))
    return np.array(im)[:, :, ::-1].copy()


def encode_face(e):
    e = e / (np.linalg.norm(e) or 1.0)
    return base64.b64encode(np.round(np.clip(e, -0.5, 0.5) * 254).astype(np.int8).tobytes()).decode()


def scan_bytes(raw):
    det, rec = get_models()
    img = to_bgr(raw)
    out = []
    for f in det.get(img):
        w = float(f.bbox[2] - f.bbox[0])
        h = float(f.bbox[3] - f.bbox[1])
        if min(w, h) < CFG['min_face'] or float(f.det_score) < CFG['det_thresh']:
            continue
        out.append({'e': encode_face(rec.get(img, f)), 's': int(round(w)), 'q': round(float(f.det_score), 2)})
    return out


# ---------------------------------------------------------------- main
def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def run(a):
    t0 = time.time()

    def out_of_time():
        return (time.time() - t0) / 60 > a.max_minutes

    CFG.update(max_side=a.max_side, det_size=a.det_size, det_thresh=a.det_thresh, min_face=a.min_face)
    idx_folder = a.index_folder
    roots = [r.strip() for r in a.root.split(',') if r.strip()]
    events, skipped = discover(roots)
    for s in skipped:
        print('ข้าม:', s)
    if not events:
        print('ไม่พบงานเลย ตรวจโครงโฟลเดอร์ ปี/เดือน/งาน อีกครั้ง')
        return 1
    events.sort(key=lambda e: (-e['year'], -e['month'], e['raw']))  # งานล่าสุดก่อน
    if a.only_contains:
        events = [e for e in events if a.only_contains in e['raw']]
    print('พบ %d งาน' % len(events))
    if a.list_only:
        for e in events:
            print('  พ.ศ.%d เดือน %d · %s' % (e['year'], e['month'], e['raw']))
        return 0

    catalog = load_json('catalog.json', idx_folder)
    if not catalog or catalog.get('engine') != ENGINE:
        catalog = {'version': 2, 'engine': ENGINE, 'events': []}
    entries = {e['id']: e for e in catalog['events']}

    def write_catalog():
        _, rec = get_models() if _models else (None, None)
        if rec is not None:
            catalog['norm'] = [float(rec.input_mean), float(rec.input_std)]
        catalog['min_face'] = CFG['min_face']
        catalog['events'] = list(entries.values())
        catalog['updated'] = now_iso()
        save_json('catalog.json', catalog, idx_folder)

    changed = False
    for ev in events:
        if out_of_time():
            print('ครบเวลาที่ตั้งไว้ หยุดพัก (จะทำต่อในรอบหน้า)')
            break
        name, date = parse_event(ev['raw'])
        fname = ev['folder'] + '.arc.json'
        cur = images_in(ev['folder'])
        cur_ids = {p['id'] for p in cur}
        idx = load_json(fname, idx_folder) or {'version': 2, 'engine': ENGINE, 'photos': []}
        old = {p['id']: p for p in idx.get('photos', [])}
        todo = [p for p in cur if p['id'] not in old]
        removed = [i for i in old if i not in cur_ids]
        known = ev['folder'] in entries
        if not todo and not removed and known:
            continue
        print('%s / เดือน %d / %s: ใหม่ %d · เดิม %d · ถูกลบ %d' % (ev['year'], ev['month'], name, len(todo),
                                                               len(old) - len(removed), len(removed)))
        fresh, i = {}, 0
        for p in todo:
            if out_of_time():
                print('  ครบเวลา หยุดพัก')
                break
            i += 1
            try:
                raw = get_bytes(p['id'])
            except Exception as ex:  # noqa  ดาวน์โหลดไม่ได้: ไม่บันทึก เพื่อลองใหม่รอบหน้า
                print('  โหลดไม่ได้ %s -> %s' % (p['name'], ex))
                continue
            try:
                fresh[p['id']] = {'faces': scan_bytes(raw)}
            except Exception as ex:  # noqa  ไฟล์เสีย: บันทึกว่าเสีย จะได้ไม่ลองซ้ำ
                print('  ข้ามรูป %s -> %s' % (p['name'], ex))
                fresh[p['id']] = {'faces': [], 'error': True}
            if i % 25 == 0:
                print('  %d/%d' % (i, len(todo)), flush=True)
            if i % 100 == 0:
                idx['photos'] = _build(cur, old, fresh)
                save_json(fname, idx, idx_folder)
        idx.update({'engine': ENGINE, 'year': ev['year'], 'month': ev['month'], 'event': name, 'date': date})
        idx['photos'] = _build(cur, old, fresh)
        fid = save_json(fname, idx, idx_folder)
        entries[ev['folder']] = {'id': ev['folder'], 'file': fname, 'year': ev['year'], 'month': ev['month'],
                                 'event': name, 'name': name, 'date': date, 'index': fid,
                                 'photos': len(idx['photos']),
                                 'faces': sum(len(p['faces']) for p in idx['photos'])}
        write_catalog()
        changed = True
    if not changed:
        print('ไม่มีรูปใหม่ ไม่มีอะไรต้องอัปเดต')
    else:
        print('เสร็จ: %d งานในดัชนี' % len(entries))
    return 0


def _build(cur, old, fresh):
    out = []
    for p in cur:
        e = old.get(p['id']) or fresh.get(p['id'])
        if e:
            item = {'id': p['id'], 'name': p['name'], 'faces': e['faces']}
            if e.get('error'):
                item['error'] = True
            out.append(item)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', required=True, help='ID โฟลเดอร์รากหรือโฟลเดอร์ปี (หลายอันคั่นด้วย ,)')
    ap.add_argument('--index-folder', required=True, help='ID โฟลเดอร์ _faceindex_arc')
    ap.add_argument('--max-minutes', type=float, default=300)
    ap.add_argument('--max-side', type=int, default=2400)
    ap.add_argument('--det-size', type=int, default=1600)
    ap.add_argument('--det-thresh', type=float, default=0.5)
    ap.add_argument('--min-face', type=int, default=40)
    ap.add_argument('--only-contains', default='')
    ap.add_argument('--list-only', action='store_true')
    return run(ap.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
