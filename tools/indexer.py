#!/usr/bin/env python3
"""
สแกนรูปกิจกรรมโรงเรียนแล้วสร้างไฟล์ดัชนีใบหน้าให้หน้าเว็บ "ค้นหารูปของฉัน"

โครงโฟลเดอร์ที่คาดหวัง:   <ราก> / <ปี พ.ศ.> / <เดือน> / <ชื่องาน> / รูป...
  - ปี   : "2569" หรือ "พ.ศ. 2569" (ถ้าเป็น ค.ศ. เช่น 2026 จะแปลงเป็น พ.ศ. ให้)
  - เดือน : "10", "ตุลาคม", "10 ตุลาคม", "ต.ค." หรือชื่อเดือนอังกฤษ
  - โฟลเดอร์ที่ขึ้นต้นด้วย _ (เช่น _faceindex) จะถูกข้าม

ผลลัพธ์ (ในโฟลเดอร์ดัชนี):
  catalog.json                     รายการงานทั้งหมด
  <ปี>-<เดือน>-<ชื่องาน>.json       ดัชนีของแต่ละงาน  {year, month, event, photos:[{id,name,faces:[[128 ค่า]]}]}

รันซ้ำได้ปลอดภัย: สแกนเฉพาะรูปใหม่ และตัดรูปที่ถูกลบออกจากดัชนี
ใช้ face-api.js + โมเดลชุดเดียวกับหน้าเว็บ (ผ่าน Chromium แบบไม่มีหน้าจอ) เวกเตอร์จึงเทียบกันได้ตรงกัน
"""
import argparse, base64, collections, concurrent.futures as cf, datetime, functools, http.server
import io, json, re, socketserver, sys, threading
from pathlib import Path
from PIL import Image, ImageOps
try:
    import pillow_heif; pillow_heif.register_heif_opener()
except Exception:
    pass

MAX_SIDE = 1280
IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'}
IMG_MIME = {'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'}
FOLDER_MIME = 'application/vnd.google-apps.folder'

TH_MONTHS = ['มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน', 'พฤษภาคม', 'มิถุนายน',
             'กรกฎาคม', 'สิงหาคม', 'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม']
TH_ABBR = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.']
EN_MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']


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


# ---------------------------------------------------------------- sources
class LocalSource:
    """โฟลเดอร์ในเครื่อง (ใช้ทดสอบ หรือรันนอก Colab)"""
    root_id = ''

    def __init__(self, root):
        self.root = Path(root)
        self.roots = [r for r in str(root).split(',') if r.strip()]
        self.root = Path(self.roots[0])  # ทดสอบในเครื่อง: ใช้ราก/โฟลเดอร์ปีเดียว

    def name_of(self, rid):
        return (self.root / rid).resolve().name

    def list(self, rel):
        out = []
        for p in sorted((self.root / rel).iterdir(), key=lambda p: p.name):
            rid = str(Path(rel) / p.name) if rel else p.name
            if p.is_dir():
                out.append({'id': rid, 'name': p.name, 'dir': True})
            elif p.suffix.lower() in IMG_EXT and not p.name.startswith('.'):
                out.append({'id': rid, 'name': p.name, 'dir': False})
        return out

    def read(self, rid):
        return (self.root / rid).read_bytes()


class DriveSource:
    """Google Drive (ใช้ใน Colab หลัง auth.authenticate_user())"""

    def __init__(self, root_id):
        import google.auth
        self.creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive'])
        self.roots = [r.strip() for r in root_id.split(',') if r.strip()]  # ใส่ได้หลายโฟลเดอร์ คั่นด้วย ,
        self.root_id = self.roots[0]
        self._tl = threading.local()

    def svc(self):
        s = getattr(self._tl, 's', None)
        if s is None:
            from googleapiclient.discovery import build
            s = self._tl.s = build('drive', 'v3', credentials=self.creds, cache_discovery=False)
        return s

    def name_of(self, fid):
        return self.svc().files().get(fileId=fid, fields='name', supportsAllDrives=True).execute(num_retries=5)['name']

    def _list_raw(self, fid):
        tok = None
        while True:
            r = self.svc().files().list(
                q="'%s' in parents and trashed=false" % fid,
                fields='nextPageToken,files(id,name,mimeType)', pageSize=1000, pageToken=tok,
                supportsAllDrives=True, includeItemsFromAllDrives=True).execute(num_retries=5)
            yield from r.get('files', [])
            tok = r.get('nextPageToken')
            if not tok:
                return

    def list(self, fid):
        out = []
        for f in sorted(self._list_raw(fid), key=lambda f: f['name']):
            if f['mimeType'] == FOLDER_MIME:
                out.append({'id': f['id'], 'name': f['name'], 'dir': True})
            elif f['mimeType'] in IMG_MIME or Path(f['name']).suffix.lower() in IMG_EXT:
                out.append({'id': f['id'], 'name': f['name'], 'dir': False})
        return out

    def read(self, fid):
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, self.svc().files().get_media(fileId=fid, supportsAllDrives=True),
                                 chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk(num_retries=5)
        return buf.getvalue()


# ---------------------------------------------------------------- stores (where index json lives)
def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class LocalStore:
    def __init__(self, d):
        self.d = Path(d)
        self.d.mkdir(parents=True, exist_ok=True)

    def read(self, name):
        p = self.d / name
        return json.loads(p.read_text('utf-8')) if p.exists() else None

    def write(self, name, obj):
        tmp = self.d / (name + '.tmp')
        tmp.write_bytes(_dump(obj))
        tmp.replace(self.d / name)


class DriveStore:
    def __init__(self, src, folder_id=None):
        self.src = src
        if not folder_id:
            for it in src.list(src.root_id):
                if it['dir'] and it['name'] == '_faceindex':
                    folder_id = it['id']
                    break
            else:
                try:
                    folder_id = src.svc().files().create(
                        body={'name': '_faceindex', 'mimeType': FOLDER_MIME, 'parents': [src.root_id]},
                        fields='id', supportsAllDrives=True).execute()['id']
                except Exception as e:
                    raise SystemExit('สร้างโฟลเดอร์ _faceindex ในโฟลเดอร์รูปไม่ได้ (อาจไม่มีสิทธิ์แก้ไข): %s\n'
                                     'แก้: สร้างโฟลเดอร์ _faceindex ในไดรฟ์ของคุณเอง แล้วใส่ ID ที่ INDEX_FOLDER_ID' % e)
                print('สร้างโฟลเดอร์ _faceindex แล้ว (id: %s) — อย่าลืมแชร์เป็น "ทุกคนที่มีลิงก์ = ผู้ดู"' % folder_id)
        self.folder_id = folder_id
        self.ids = {f['name']: f['id'] for f in src._list_raw(folder_id)}

    def read(self, name):
        fid = self.ids.get(name)
        if not fid:
            return None
        return json.loads(self.src.read(fid).decode('utf-8'))

    def write(self, name, obj):
        from googleapiclient.http import MediaIoBaseUpload
        import time
        data = _dump(obj)
        last = None
        for attempt in range(6):
            try:
                # สร้างการเชื่อมต่อใหม่ทุกครั้ง (ของเดิมอาจหมดอายุระหว่างสแกนนาน ๆ -> Broken pipe)
                self.src._tl.s = None
                s = self.src.svc()
                media = MediaIoBaseUpload(io.BytesIO(data), mimetype='application/json', resumable=True)
                if name in self.ids:
                    s.files().update(fileId=self.ids[name], media_body=media, supportsAllDrives=True).execute(num_retries=5)
                else:
                    self.ids[name] = s.files().create(
                        body={'name': name, 'parents': [self.folder_id]}, media_body=media,
                        fields='id', supportsAllDrives=True).execute(num_retries=5)['id']
                return
            except Exception as e:  # noqa
                last = e
                print('อัปโหลด %s ไม่สำเร็จ (ครั้งที่ %d): %s — ลองใหม่' % (name, attempt + 1, e))
                time.sleep(2 * (attempt + 1))
        raise last


# ---------------------------------------------------------------- face scanner (headless Chromium + face-api.js)
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class Scanner:
    def __init__(self, site_dir):
        from playwright.sync_api import sync_playwright
        handler = functools.partial(_Quiet, directory=str(site_dir))
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.httpd = socketserver.ThreadingTCPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader',
                                                     '--ignore-gpu-blocklist'])
        self.page = self.browser.new_page()
        self.page.on('pageerror', lambda e: print('[harness]', e))
        self.page.goto('http://127.0.0.1:%d/tools/indexer_harness.html' % self.httpd.server_address[1])
        self.backend = self.page.evaluate('window.ready')

    def scan(self, b64):
        return self.page.evaluate('b => scan(b)', b64)

    def close(self):
        for f in (self.browser.close, self.pw.stop, self.httpd.shutdown):
            try:
                f()
            except Exception:
                pass


def prep(data):
    """EXIF หมุนให้ถูก + ย่อด้านยาวไม่เกิน 1280 (เหมือนที่หน้าเว็บทำ) -> base64 JPEG"""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def iter_prepared(src, items, workers):
    """ดาวน์โหลด+ย่อรูปล่วงหน้าแบบขนาน (จำกัดจำนวนที่ค้างในหน่วยความจำ) คืนค่าตามลำดับเดิม"""
    def work(it):
        try:
            return it, prep(src.read(it['id'])), None
        except Exception as e:  # noqa
            return it, None, e
    with cf.ThreadPoolExecutor(workers) as ex:
        pending, it = collections.deque(), iter(items)

        def feed():
            nxt = next(it, None)
            if nxt is not None:
                pending.append(ex.submit(work, nxt))
        for _ in range(workers * 2):
            feed()
        while pending:
            r = pending.popleft().result()
            feed()
            yield r


# ---------------------------------------------------------------- discovery
def discover(src):
    """รับได้ทั้ง 'โฟลเดอร์ราก' (มีโฟลเดอร์ปีอยู่ข้างใน) และ 'โฟลเดอร์ปี' เดี่ยวๆ (มีโฟลเดอร์เดือนข้างใน)
    ใส่ได้หลายโฟลเดอร์ เช่น 2569 กับ 2570 ที่แยกกัน"""
    events, skipped = [], []

    def add_year(year, yid):
        for m in src.list(yid):
            if not m['dir'] or m['name'].startswith('_'):
                continue
            month = parse_month(m['name'])
            if month is None:
                skipped.append('โฟลเดอร์เดือนที่อ่านไม่ออก: %d/%s' % (year, m['name']))
                continue
            for e in src.list(m['id']):
                if e['dir'] and not e['name'].startswith('_'):
                    events.append({'year': year, 'month': month, 'name': e['name'], 'folder': e['id']})

    for rid in src.roots:
        kids = [k for k in src.list(rid) if k['dir'] and not k['name'].startswith('_')]
        own_year = parse_year(src.name_of(rid))
        if own_year is not None and not any(parse_year(k['name']) for k in kids):
            print('ใช้ "%s" เป็นโฟลเดอร์ปี พ.ศ.%d' % (src.name_of(rid), own_year))
            add_year(own_year, rid)
            continue
        for y in kids:
            year = parse_year(y['name'])
            if year is None:
                skipped.append('โฟลเดอร์ปีที่อ่านไม่ออก: ' + y['name'])
                continue
            add_year(year, y['id'])
    return events, skipped


def collect_photos(src, folder):
    out, stack = [], [folder]
    while stack:
        for it in src.list(stack.pop()):
            if it['dir']:
                if not it['name'].startswith('_'):
                    stack.append(it['id'])
            else:
                out.append(it)
    out.sort(key=lambda p: (p['name'], p['id']))
    return out


def safe(s):
    return re.sub(r'[\\/:*?"<>|\s]+', '_', s).strip('_') or 'event'


# ---------------------------------------------------------------- main
def run(a):
    if a.source == 'drive':
        src = DriveSource(a.root)
        store = DriveStore(src, a.index_folder or None)
    else:
        src = LocalSource(a.root)
        store = LocalStore(a.out)

    events, skipped = discover(src)
    for s in skipped:
        print('ข้าม:', s)
    if not events:
        print('ไม่พบงานเลย ตรวจโครงโฟลเดอร์ ปี/เดือน/งาน อีกครั้ง')
        return 1

    plan, used = [], set()
    for ev in events:
        photos = collect_photos(src, ev['folder'])
        base = '%d-%02d-%s' % (ev['year'], ev['month'], safe(ev['name']))
        fname, n = base + '.json', 2
        while fname in used:  # ชื่องานซ้ำในเดือนเดียวกัน
            fname = '%s-%d.json' % (base, n)
            n += 1
        used.add(fname)
        plan.append((ev, fname, photos))
        print('พบงาน: พ.ศ.%d เดือน %d · %s · %d รูป' % (ev['year'], ev['month'], ev['name'], len(photos)))
    if a.list_only:
        return 0

    sc = Scanner(a.site)
    print('เครื่องมือสแกนพร้อม (backend: %s)' % sc.backend)
    catalog, failed_all = [], []
    try:
        for ev, fname, photos in plan:
            if not photos:
                print('ข้ามงานที่ไม่มีรูป:', ev['name'])
                continue
            old = store.read(fname) or {}
            oldmap = {p['id']: p for p in old.get('photos', []) if p.get('id')}
            cur = {p['id']: p for p in photos}
            todo = [p for p in photos if p['id'] not in oldmap]
            removed = [i for i in oldmap if i not in cur]
            fresh, failed = {}, []

            def build():
                out = []
                for p in photos:
                    e = oldmap.get(p['id']) or fresh.get(p['id'])
                    if e:
                        out.append({'id': p['id'], 'name': p['name'], 'faces': e['faces']})
                return {'version': 1, 'year': ev['year'], 'month': ev['month'], 'event': ev['name'], 'photos': out}

            print('งาน %s: ใหม่ %d · เดิม %d · ถูกลบ %d' % (ev['name'], len(todo), len(oldmap) - len(removed), len(removed)))
            for i, (p, b64, err) in enumerate(iter_prepared(src, todo, a.workers), 1):
                if err is not None:
                    failed.append(p['name'])
                    print('  อ่านไม่ได้: %s (%s)' % (p['name'], err))
                    continue
                try:
                    fresh[p['id']] = {'faces': sc.scan(b64)}
                except Exception as e:  # noqa
                    failed.append(p['name'])
                    print('  สแกนไม่ได้: %s (%s)' % (p['name'], e))
                if i % 10 == 0 or i == len(todo):
                    print('  สแกนแล้ว %d/%d' % (i, len(todo)), flush=True)
                if i % 100 == 0:  # กันเน็ตหลุด/เซสชันหมดอายุ: บันทึกระหว่างทาง
                    store.write(fname, build())
            doc = build()
            if todo or removed or not old:
                store.write(fname, doc)
            catalog.append({'file': fname, 'year': ev['year'], 'month': ev['month'], 'event': ev['name'],
                            'photos': len(doc['photos']), 'faces': sum(len(p['faces']) for p in doc['photos'])})
            failed_all += ['%s/%s' % (ev['name'], f) for f in failed]
            # เขียน catalog ระหว่างทางทุกงาน: ถ้าสแกนหลุดกลางคัน เว็บก็ยังเห็นงานที่เสร็จแล้ว
            try:
                store.write('catalog.json', {'version': 1, 'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                                             'events': sorted(catalog, key=lambda c: (-c['year'], -c['month'], c['event']))})
            except Exception as e:  # noqa
                print('  (บันทึก catalog ระหว่างทางไม่สำเร็จ จะลองใหม่ตอนจบ: %s)' % e)
    finally:
        sc.close()

    catalog.sort(key=lambda c: (-c['year'], -c['month'], c['event']))
    store.write('catalog.json', {'version': 1, 'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                                 'events': catalog})
    print('เสร็จ: %d งาน, %d รูป, %d ใบหน้า' % (len(catalog), sum(c['photos'] for c in catalog), sum(c['faces'] for c in catalog)))
    if failed_all:
        print('รูปที่อ่านไม่ได้ (จะลองใหม่ในการรันครั้งหน้า): %d ไฟล์' % len(failed_all))
        for f in failed_all[:30]:
            print('  -', f)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', choices=['drive', 'local'], required=True)
    ap.add_argument('--root', required=True, help='drive: ID โฟลเดอร์รากหรือโฟลเดอร์ปี (ใส่หลายอันคั่นด้วย ,) | local: พาธโฟลเดอร์')
    ap.add_argument('--index-folder', default='', help='drive: ID โฟลเดอร์เก็บดัชนี (ว่าง = หา/สร้าง _faceindex ใต้ราก)')
    ap.add_argument('--out', default='index-out', help='local: โฟลเดอร์ที่เขียนดัชนี')
    ap.add_argument('--site', default=str(Path(__file__).resolve().parent.parent), help='โฟลเดอร์เว็บ (มี face-api.js และ models/)')
    ap.add_argument('--workers', type=int, default=4, help='จำนวนเธรดดาวน์โหลดรูป')
    ap.add_argument('--list-only', action='store_true', help='แสดงงานที่พบเท่านั้น ไม่สแกน')
    return run(ap.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
