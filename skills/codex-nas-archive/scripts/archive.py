#!/usr/bin/env python3
"""Local-only Codex NAS archive. Python standard library; no Codex DB writes."""
import argparse
import calendar
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MEDIA = {'.png','.jpg','.jpeg','.webp','.gif','.avif','.heic','.tiff','.bmp','.mp4','.mov','.mkv','.webm','.m4v','.mp3','.wav','.m4a','.flac','.ogg','.aac'}
VERSION = 3


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def signature(path):
    s = Path(path).lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError('只处理普通、非硬链接文件')
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def safe_path(path):
    raw = Path(path).expanduser()
    if '..' in raw.parts:
        raise ValueError('路径不能包含上级目录跳转 ..')
    p = raw.absolute()
    for part in [p, *p.parents]:
        if part.is_symlink():
            raise ValueError('路径包含符号链接，已跳过：' + str(part))
    return p


def beneath(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def cutoff(period, now=None):
    now = now or dt.datetime.now().astimezone()
    n, unit = int(period['value']), period['unit']
    if n < 1 or n > 3650 or unit not in ('days', 'months'):
        raise ValueError('期限应为 1–3650 天或月')
    if unit == 'days':
        return (now - dt.timedelta(days=n)).timestamp()
    if n > 120:
        raise ValueError('月份上限为 120')
    month = now.year * 12 + now.month - 1 - n
    year, month = divmod(month, 12)
    return now.replace(year=year, month=month+1, day=min(now.day, calendar.monthrange(year, month+1)[1])).timestamp()


def atomic_json(path, value):
    tmp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        with open(tmp, 'x', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Archive:
    def __init__(self, state=None, test_local=False):
        self.state = Path(state or os.environ.get('CODEX_NAS_ARCHIVE_HOME', Path.home()/'.local/share/codex-nas-archive')).expanduser().absolute()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.test_local = test_local
        self.config_path = self.state/'config.json'
        self.dbpath = self.state/'index.sqlite'
        with self.lock(), self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files (
              id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, thread_id TEXT, title TEXT,
              size INTEGER, allocated INTEGER, mtime REAL, mode INTEGER, status TEXT,
              reason TEXT DEFAULT '', sha256 TEXT, object TEXT, destination TEXT,
              verified_at REAL, released_at REAL, updated_at REAL, excluded INTEGER DEFAULT 0,
              eligible INTEGER DEFAULT 0, release_temp TEXT);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, file_id TEXT, action TEXT, detail TEXT);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in db.execute('PRAGMA table_info(files)')}
            if 'last_accessed_at' not in columns:
                db.execute('ALTER TABLE files ADD COLUMN last_accessed_at REAL')
                db.execute('ALTER TABLE files ADD COLUMN access_source TEXT')
                db.execute('ALTER TABLE files ADD COLUMN access_observed_since REAL')
                # Older archives were not monitored: begin observation at upgrade,
                # retaining any successful restore events already present in the log.
                db.execute('UPDATE files SET access_observed_since=? WHERE object IS NOT NULL', (time.time(),))
                db.execute("""UPDATE files SET last_accessed_at=(
                    SELECT MAX(time) FROM events WHERE file_id=files.id AND action IN ('restored','already-local'))
                    WHERE object IS NOT NULL""")
                db.execute("UPDATE files SET access_source='restore' WHERE last_accessed_at IS NOT NULL")
            if 'nas_deleted_at' not in columns:
                db.execute('ALTER TABLE files ADD COLUMN nas_deleted_at REAL')
                db.execute('ALTER TABLE files ADD COLUMN delete_previous_status TEXT')
            db.execute('CREATE TABLE IF NOT EXISTS delete_previews (token TEXT PRIMARY KEY, expires REAL, payload TEXT)')
            db.execute('PRAGMA user_version=3')

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.dbpath, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @contextlib.contextmanager
    def lock(self):
        with open(self.state/'operation.lock', 'a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('另一个归档操作正在运行，请稍后重试')
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def config(self):
        if self.config_path.exists():
            c = json.loads(self.config_path.read_text())
            c.setdefault('cleanup_enabled', True)
            c.setdefault('cleanup_period', {'value':6,'unit':'months'})
            return c
        return {'version':VERSION, 'roots':[str(Path.home()/'Documents/Codex')], 'period':{'value':1,'unit':'months'},
                'nas_url':'', 'nas_path':'', 'destination_id':'', 'grace_days':7, 'cleanup_enabled':True, 'cleanup_period':{'value':6,'unit':'months'},
                'codex_home':str(Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))), 'configured':False}

    def save_config(self, data):
        with self.lock():
            old = self.config()
            c = dict(old)
            for key in ('roots','period','nas_url','nas_path','grace_days','cleanup_enabled','cleanup_period'):
                if key in data:
                    c[key] = data[key]
            cutoff(c['period'])
            cutoff(c['cleanup_period'])
            if not isinstance(c['cleanup_enabled'], bool):
                raise ValueError('清理建议开关必须为布尔值')
            c['grace_days'] = int(c['grace_days'])
            if not 0 <= c['grace_days'] <= 365:
                raise ValueError('本地缓冲期应为 0–365 天')
            if not isinstance(c['roots'], list) or not c['roots']:
                raise ValueError('至少填写一个扫描目录')
            c['roots'] = [str(safe_path(p)) for p in c['roots'] if p]
            if not c['roots'] or any(not Path(p).is_dir() for p in c['roots']):
                raise ValueError('扫描目录不存在')
            if c['nas_url']:
                u = urlsplit(c['nas_url'])
                if u.scheme != 'smb' or not u.hostname or not u.path.strip('/') or u.username or u.password or u.query or u.fragment:
                    raise ValueError('请填写 smb://主机/共享名，不要包含账号或密码')
            if c['nas_path']:
                c['nas_path'] = str(safe_path(c['nas_path']))
                if any(beneath(c['nas_path'], r) or beneath(r, c['nas_path']) for r in c['roots']):
                    raise ValueError('NAS 目录不能与扫描目录重叠')
            if c['nas_path'] != old.get('nas_path'):
                with self.db() as db:
                    if db.execute("SELECT 1 FROM files WHERE object IS NOT NULL LIMIT 1").fetchone():
                        raise ValueError('已有归档时不能切换目标；请先恢复全部文件并保留旧索引，或使用独立配置目录')
                c['destination_id'] = ''
            c['version'] = VERSION
            c['configured'] = bool(data.get('configured', old.get('configured', False)))
            if c['period'] != old['period'] or c['roots'] != old['roots']:
                with self.db() as db:
                    db.execute("UPDATE files SET eligible=0,status='skipped',reason='归档规则已变化，请重新扫描' WHERE status IN ('pending','skipped','excluded','error')")
            atomic_json(self.config_path, c)
            return c

    def mount_check(self, path):
        p = safe_path(path)
        if not p.is_dir():
            raise ValueError('NAS 目录不存在；先在 Finder 挂载共享并创建归档目录')
        if self.test_local:
            return
        lines = subprocess.run(['mount'], capture_output=True, text=True, check=True).stdout.splitlines()
        mounts = []
        for line in lines:
            if ' on ' in line and ' (' in line:
                point = line.split(' on ',1)[1].split(' (',1)[0]
                fs = line.split(' (',1)[1].split(',',1)[0].rstrip(')')
                if ' type ' in point:  # Linux mount output
                    point, fs = point.rsplit(' type ',1)
                if fs in ('smbfs','cifs','nfs','nfs4','afpfs') and beneath(p, point):
                    mounts.append(point)
        if not mounts:
            raise ValueError('目标不是已识别的 SMB/NFS/AFP 网络挂载；不会写入本地同名目录')

    def destination(self, c=None):
        c = c or self.config()
        if not c.get('nas_path') or not c.get('destination_id'):
            raise ValueError('请先完成 NAS 连接验证')
        root = safe_path(c['nas_path'])
        self.mount_check(root)
        marker = safe_path(root/'.codex-archive-destination.json')
        if json.loads(marker.read_text()).get('id') != c['destination_id']:
            raise ValueError('NAS 身份标识不匹配，已停止')
        return root

    def connect(self):
        with self.lock():
            c = self.config()
            if not c['nas_path']:
                raise ValueError('请先填写并保存 NAS 挂载目录')
            root = safe_path(c['nas_path']); self.mount_check(root)
            marker = safe_path(root/'.codex-archive-destination.json')
            if marker.exists():
                ident = json.loads(marker.read_text())['id']
                if c.get('destination_id') and c['destination_id'] != ident:
                    raise ValueError('NAS 身份不匹配')
            elif c.get('destination_id'):
                raise ValueError('已绑定的 NAS 标识丢失；请检查挂载是否正确')
            else:
                ident = secrets.token_hex(16)
                with open(marker, 'x') as f:
                    json.dump({'id':ident,'version':VERSION}, f); f.flush(); os.fsync(f.fileno())
            probe = root/('.probe-'+secrets.token_hex(8))
            payload = secrets.token_bytes(4096)
            try:
                with open(probe,'xb') as f:
                    f.write(payload); f.flush(); os.fsync(f.fileno())
                if probe.read_bytes() != payload:
                    raise ValueError('NAS 写入读取校验失败')
            finally:
                probe.unlink(missing_ok=True)
            c['destination_id'] = ident
            atomic_json(self.config_path,c)
            return {'message':'NAS 写入、读取和删除测试通过', 'free':shutil.disk_usage(root).free}

    def threads(self):
        c = self.config()
        home = Path(c['codex_home'])
        candidates = sorted(home.glob('state_*.sqlite'), key=lambda p:int(p.stem.split('_')[-1]), reverse=True)
        if not candidates:
            raise ValueError('未找到 Codex 会话数据库，扫描已停止')
        conn = sqlite3.connect(candidates[0].as_uri()+'?mode=ro',uri=True,timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            columns = {r[1] for r in conn.execute('PRAGMA table_info(threads)')}
            if not {'id','cwd','title','updated_at','is_pinned'}.issubset(columns):
                raise ValueError('Codex 会话格式已变化，需更新只读适配器')
            rows = [dict(r) for r in conn.execute('SELECT id,cwd,title,updated_at,is_pinned FROM threads')]
        finally:
            conn.close()
        groups = {}
        for r in rows:
            cwd = str(Path(r['cwd']).expanduser().absolute())
            groups.setdefault(cwd,[]).append(r)
        return groups

    def eligibility(self, path, groups=None):
        c = self.config(); p = safe_path(path)
        if not any(beneath(p,r) for r in c['roots']):
            raise ValueError('文件不在配置的扫描范围')
        if p.suffix.lower() not in MEDIA:
            raise ValueError('首版只归档媒体产出')
        groups = groups if groups is not None else self.threads()
        possible = [(cwd, rows) for cwd,rows in groups.items() if beneath(p,Path(cwd)/'outputs')]
        if not possible:
            raise ValueError('找不到文件关联会话')
        # Nested working directories can refer to the same file: never infer sole ownership.
        if len(possible) != 1 or len(possible[0][1]) != 1:
            raise ValueError('目录被多个会话共享，暂不自动归档')
        row = possible[0][1][0]
        if row['is_pinned']:
            raise ValueError('会话已置顶，保留本地')
        if row['updated_at'] >= cutoff(c['period']):
            raise ValueError('会话最近仍有更新')
        s = p.lstat(); signature(p)
        if s.st_mtime >= cutoff(c['period']):
            raise ValueError('文件最近仍有更新')
        return row

    def scan(self):
        with self.lock():
            groups = self.threads(); c = self.config(); found = 0; skipped_dirs = 0
            with self.db() as db:
                db.execute('UPDATE files SET eligible=0')
            seen = set()
            for cwd, rows in groups.items():
                out = Path(cwd)/'outputs'
                if not any(beneath(out,r) for r in c['roots']) or not out.is_dir():
                    continue
                try:
                    safe_path(out)
                except ValueError:
                    skipped_dirs += 1; continue
                for current, dirs, names in os.walk(out, followlinks=False):
                    dirs[:] = [d for d in dirs if not (Path(current)/d).is_symlink() and not d.startswith('.')]
                    for name in names:
                        p = Path(current)/name
                        if p in seen or p.suffix.lower() not in MEDIA or name.startswith('.'):
                            continue
                        seen.add(p)
                        try:
                            safe_path(p); s=p.lstat(); signature(p)
                        except (ValueError,OSError):
                            continue
                        ident = hashlib.sha256(str(p).encode()).hexdigest()[:24]
                        reason = ''; eligible = 1
                        try:
                            self.eligibility(p,groups)
                        except ValueError as e:
                            reason=str(e); eligible=0
                        with self.db() as db:
                            old=db.execute('SELECT * FROM files WHERE id=?',(ident,)).fetchone()
                            if old and old['status'] in ('verified','released','releasing','restored','nas-deleted','nas-deleting'):
                                db.execute('UPDATE files SET eligible=?,reason=? WHERE id=?',(eligible,reason,ident)); continue
                            status='pending' if eligible else 'skipped'
                            if old and old['excluded']:
                                status='excluded'; eligible=0; reason='用户选择始终保留'
                            db.execute('''INSERT INTO files(id,path,thread_id,title,size,allocated,mtime,mode,status,reason,eligible,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET size=excluded.size,allocated=excluded.allocated,
                            mtime=excluded.mtime,mode=excluded.mode,status=excluded.status,reason=excluded.reason,eligible=excluded.eligible,updated_at=excluded.updated_at''',
                            (ident,str(p),rows[0]['id'],rows[0]['title'],s.st_size,getattr(s,'st_blocks',0)*512,s.st_mtime,stat.S_IMODE(s.st_mode),status,reason,eligible,time.time()))
                        found+=1
            with self.db() as db:
                db.execute("UPDATE files SET status='skipped',reason='本次扫描未找到文件或文件不再位于扫描范围' WHERE eligible=0 AND status='pending'")
            return {'message':f'扫描完成：检查 {len(seen)} 个媒体文件，跳过 {skipped_dirs} 个符号链接目录', 'scanned':len(seen)}

    def row(self, ident):
        with self.db() as db:
            row=db.execute('SELECT * FROM files WHERE id=?',(ident,)).fetchone()
        if row is None:
            raise ValueError('归档索引中没有这个文件')
        return dict(row)

    def update(self, ident, action, **fields):
        fields['updated_at']=time.time()
        with self.db() as db:
            db.execute('UPDATE files SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),ident))
            db.execute('INSERT INTO events(time,file_id,action,detail) VALUES(?,?,?,?)',(time.time(),ident,action,json.dumps(fields,ensure_ascii=False)))

    def remote_object(self, row):
        root=self.destination()
        if row['destination'] != self.config()['destination_id'] or not row['object']:
            raise ValueError('归档目标不匹配')
        p=safe_path(root/row['object'])
        if not beneath(p,root):
            raise ValueError('归档对象路径越界')
        return p

    def archive_one(self, ident):
        r=self.row(ident)
        if r['excluded']:
            raise ValueError('文件设置为始终保留')
        if r['status'] in ('verified','released','releasing'):
            return
        self.eligibility(r['path'])
        p=safe_path(r['path']); before=signature(p); s=p.stat()
        root=self.destination()
        if shutil.disk_usage(root).free < s.st_size + 16*1024*1024:
            raise ValueError('NAS 剩余空间不足')
        tmpdir=safe_path(root/'.incoming'); tmpdir.mkdir(exist_ok=True)
        tmp=tmpdir/(ident+'-'+secrets.token_hex(8))
        self.update(ident,'copy-start',status='copying',reason='')
        try:
            with open(p,'rb') as source, open(tmp,'xb') as target:
                shutil.copyfileobj(source,target,1024*1024); target.flush(); os.fsync(target.fileno())
            sha=digest(tmp)
            if before != signature(p) or digest(p) != sha or before != signature(p):
                raise ValueError('源文件在复制过程中变化，保留本地文件')
            self.destination(); self.eligibility(p)
            # Unique version directory prevents collision/overwrite and permits safe retry
            # after interruption; publish a fully copied object with a same-volume rename.
            rel=f'objects/{ident}/{sha}-{secrets.token_hex(8)}/{p.name}'
            dest=safe_path(root/rel); dest.parent.mkdir(parents=True,exist_ok=False)
            os.rename(tmp,dest)
            if digest(dest)!=sha:
                raise ValueError('NAS 最终对象校验失败')
            manifest={'version':VERSION,'id':ident,'path':str(p),'thread_id':r['thread_id'],'title':r['title'],
                      'sha256':sha,'size':s.st_size,'mtime':s.st_mtime,'mode':stat.S_IMODE(s.st_mode),'object':rel,'destination':self.config()['destination_id']}
            manifests=safe_path(root/'manifests'); manifests.mkdir(exist_ok=True)
            atomic_json(manifests/(ident+'.json'),manifest)
            self.update(ident,'verified',status='verified',sha256=sha,object=rel,destination=manifest['destination'],
                        size=s.st_size,allocated=getattr(s,'st_blocks',0)*512,mtime=s.st_mtime,mode=stat.S_IMODE(s.st_mode),verified_at=time.time(),released_at=None,reason='',last_accessed_at=None,access_source=None,access_observed_since=time.time(),nas_deleted_at=None,delete_previous_status=None)
        finally:
            tmp.unlink(missing_ok=True)

    def release_one(self, ident):
        r=self.row(ident); c=self.config()
        if r['status'] == 'released':
            return
        if r['status']!='verified' or r['excluded']:
            raise ValueError('仅清理已校验且未设为保留的文件')
        if time.time() < r['verified_at']+c['grace_days']*86400:
            raise ValueError('本地缓冲期尚未结束')
        self.eligibility(r['path'])
        p=safe_path(r['path']); before=signature(p)
        remote=self.remote_object(r)
        if digest(remote)!=r['sha256'] or digest(p)!=r['sha256'] or signature(p)!=before:
            raise ValueError('本地或 NAS 内容变化，不能清理')
        self.eligibility(p); self.destination()
        # Journal before rename. A crash retains a recoverable local sibling or a verified NAS object.
        temp=p.with_name('.'+p.name+'.codex-release-'+secrets.token_hex(8))
        self.update(ident,'release-intent',status='releasing',release_temp=str(temp))
        os.rename(p,temp)
        moved_signature=signature(temp)
        if digest(temp)!=r['sha256'] or signature(temp)!=moved_signature:
            if not p.exists():
                os.link(temp,p); temp.unlink()
            raise ValueError('清理时源文件变化；已停止')
        temp.unlink()
        self.update(ident,'released',status='released',released_at=time.time(),release_temp=None,reason='')

    def restore_one(self, ident):
        r=self.row(ident)
        if r['nas_deleted_at'] or r['status']=='nas-deleting':
            raise ValueError('NAS 归档已删除或删除状态待核对，不能从该归档恢复')
        if not r['object']:
            raise ValueError('尚未归档，没有 NAS 副本')
        p=safe_path(r['path'])
        if p.exists():
            if digest(p)==r['sha256']:
                self.update(ident,'already-local',status='restored',released_at=None,reason='',last_accessed_at=time.time(),access_source='restore'); return
            raise ValueError('原路径已有不同文件，拒绝覆盖；请先选择或备份本地版本')
        remote=self.remote_object(r)
        if digest(remote)!=r['sha256']:
            raise ValueError('NAS 副本校验失败，不能恢复')
        p.parent.mkdir(parents=True,exist_ok=True)
        safe_path(p)
        if shutil.disk_usage(p.parent).free < r['size']+16*1024*1024:
            raise ValueError('本地空间不足')
        temp=p.with_name('.'+p.name+'.codex-restore-'+secrets.token_hex(8))
        try:
            with open(remote,'rb') as source,open(temp,'xb') as target:
                shutil.copyfileobj(source,target,1024*1024); target.flush(); os.fsync(target.fileno())
            if digest(temp)!=r['sha256']:
                raise ValueError('下载校验失败')
            os.chmod(temp,r['mode']); os.utime(temp,(r['mtime'],r['mtime']))
            safe_path(p)
            os.link(temp,p)  # Atomic no-clobber publication on the local filesystem.
            self.update(ident,'restored',status='restored',released_at=None,reason='',last_accessed_at=time.time(),access_source='restore')
        finally:
            temp.unlink(missing_ok=True)

    def mark_used_one(self, ident):
        r = self.row(ident)
        if not r['object'] or not r['verified_at'] or r['nas_deleted_at']:
            raise ValueError('仅能为已归档文件登记使用记录')
        self.update(ident,'manual-access',last_accessed_at=time.time(),access_source='manual')

    DELETE_KEYS = ('id','object','sha256','destination','size','status','excluded','verified_at','last_accessed_at','nas_deleted_at')

    def local_copy_complete(self, r):
        try:
            p=safe_path(r['path']); before=signature(p)
            return digest(p)==r['sha256'] and signature(p)==before
        except (ValueError,OSError):
            return False

    def delete_check(self, r):
        if r['status'] not in ('verified','released','restored') or not r['object'] or not r['sha256'] or r['nas_deleted_at']:
            raise ValueError('只能删除状态明确且已校验的 NAS 归档')
        if r['excluded']:
            raise ValueError('文件标记为始终保留，不能删除')
        remote=self.remote_object(r)
        if not beneath(remote,self.destination()/'objects'/r['id']):
            raise ValueError('目标不是本工具管理的文件对象')
        before=signature(remote)
        if digest(remote)!=r['sha256'] or signature(remote)!=before:
            raise ValueError('NAS 文件已变化，拒绝删除，请重新核对')
        return remote,before

    def preview_delete(self, ids):
        if not isinstance(ids,list) or not ids or len(ids)>1000:
            raise ValueError('请选择 1–1000 个已归档文件')
        with self.lock():
            self.recover()
            items=[]
            for ident in dict.fromkeys(ids):
                r=self.row(ident);remote,_=self.delete_check(r)
                items.append({'id':ident,'name':Path(r['path']).name,'title':r['title'],'path':r['path'],
                    'nas_path':str(remote),'size':r['size'],'local_copy_verified':self.local_copy_complete(r),
                    'revision':{k:r[k] for k in self.DELETE_KEYS}})
            ticket=secrets.token_urlsafe(32);expires=time.time()+600
            with self.db() as db:
                db.execute('DELETE FROM delete_previews WHERE expires<?',(time.time(),))
                db.execute('INSERT INTO delete_previews VALUES(?,?,?)',(ticket,expires,json.dumps(items,ensure_ascii=False)))
            return {'message':'删除清单已生成；尚未删除文件','delete_preview':{'token':ticket,'expires_at':expires,
                'items':items,'total_bytes':sum(i['size'] for i in items),
                'without_local_copy':sum(not i['local_copy_verified'] for i in items),
                'warning':'永久删除所列 NAS 对象，不经过本工具回收站；本工具将无法从这些归档恢复。NAS 快照或回收策略可能仍占用空间。'}}

    def write_tombstone(self, r):
        root=self.destination()
        path=safe_path(root/'manifests'/(r['id']+'.json'))
        atomic_json(path,{'version':VERSION,'id':r['id'],'path':r['path'],'thread_id':r['thread_id'],
            'object':r['object'],'sha256':r['sha256'],'status':'nas-deleted','nas_deleted_at':r['nas_deleted_at']})

    def confirm_delete(self, ticket, phrase):
        if phrase!='删除 NAS 归档':
            raise ValueError('请输入完整确认文字：删除 NAS 归档')
        with self.lock():
            with self.db() as db:
                preview=db.execute('SELECT * FROM delete_previews WHERE token=?',(ticket,)).fetchone()
                if not preview or preview['expires']<time.time():
                    raise ValueError('确认清单不存在、已使用或已过期，请重新预览')
                items=json.loads(preview['payload'])
                # Consume before checking: any failure requires a fresh review.
                db.execute('DELETE FROM delete_previews WHERE token=?',(ticket,))
            ready=[]
            for item in items:
                r=self.row(item['id'])
                if {k:r[k] for k in self.DELETE_KEYS}!=item['revision']:
                    raise ValueError('文件使用记录、保留状态或归档版本变化，请重新预览确认')
                remote,before=self.delete_check(r)
                if item['local_copy_verified'] and not self.local_copy_complete(r):
                    raise ValueError('预览时的本地完整副本已变化或消失，请重新预览确认')
                ready.append((r,remote,before,item['local_copy_verified']))
            results=[]
            for r,remote,before,had_local_copy in ready:
                try:
                    self.destination()
                    if had_local_copy and not self.local_copy_complete(r):
                        raise ValueError('本地完整副本在确认后变化，已停止该文件删除')
                    if signature(remote)!=before:
                        raise ValueError('NAS 文件在确认后变化，已停止该文件删除')
                    self.update(r['id'],'nas-delete-intent',status='nas-deleting',delete_previous_status=r['status'])
                    remote.unlink()
                    self.update(r['id'],'nas-deleted',status='nas-deleted',nas_deleted_at=time.time(),eligible=0,reason='用户确认后删除 NAS 归档')
                    try:
                        self.write_tombstone(self.row(r['id']))
                    except Exception as e:
                        self.update(r['id'],'tombstone-warning',reason='NAS 文件已删除；远端记录更新失败：'+str(e))
                    results.append({'id':r['id'],'ok':True})
                except Exception as e:
                    # Keep journaled uncertainty: never claim deletion or retry it silently.
                    self.update(r['id'],'nas-delete-failed',reason=str(e))
                    results.append({'id':r['id'],'ok':False,'error':str(e)})
            return {'message':f"已删除 {sum(r['ok'] for r in results)}/{len(results)} 个 NAS 归档；本地文件未删除",'results':results}

    def recover(self):
        with self.db() as db:
            uncertain=[dict(r) for r in db.execute("SELECT * FROM files WHERE status='nas-deleting'")]
        for r in uncertain:
            remote=self.remote_object(r)
            try:
                remote.lstat()
            except FileNotFoundError:
                self.update(r['id'],'nas-delete-reconciled',status='nas-deleted',nas_deleted_at=time.time(),eligible=0,reason='确认删除曾中断；已核对 NAS 对象不存在')
                try:
                    self.write_tombstone(self.row(r['id']))
                except Exception:
                    pass
            else:
                self.update(r['id'],'nas-delete-cancelled',status=r['delete_previous_status'],reason='删除曾中断，NAS 文件仍在；需重新预览确认')
        with self.db() as db:
            interrupted=[dict(r) for r in db.execute("SELECT * FROM files WHERE status IN ('releasing','copying')")]
        for r in interrupted:
            if r['status']=='copying':
                self.update(r['id'],'interrupted',status='error',reason='复制曾中断，可重试；本地文件未清理'); continue
            p=safe_path(r['path']); temp=safe_path(r['release_temp'])
            if temp.exists():
                if p.exists():
                    continue  # Preserve both; dashboard explains pending manual recovery.
                os.link(temp,p); temp.unlink()
                self.update(r['id'],'release-recovered',status='verified',release_temp=None,reason='中断清理已恢复本地文件')
            elif p.exists():
                self.update(r['id'],'release-cancelled',status='verified',release_temp=None)
            else:
                self.update(r['id'],'release-reconciled',status='released',released_at=time.time(),release_temp=None)

    def batch(self, action, ids):
        if action not in ('archive','release','restore','exclude','include','mark-used') or not isinstance(ids,list) or not ids or len(ids)>1000:
            raise ValueError('请选择 1–1000 个文件和有效操作')
        results=[]
        with self.lock():
            self.recover()
            for ident in dict.fromkeys(ids):
                try:
                    if action in ('exclude','include'):
                        r=self.row(ident)
                        fields={'excluded':int(action=='exclude')}
                        if action=='include' and r['status']=='excluded':
                            try:
                                self.eligibility(r['path'])
                                fields.update(status='pending',eligible=1,reason='')
                            except (ValueError,OSError) as e:
                                fields.update(status='skipped',eligible=0,reason=str(e))
                        self.update(ident,action,**fields)
                    else:
                        getattr(self,action.replace('-','_')+'_one')(ident)
                    results.append({'id':ident,'ok':True})
                except Exception as e:
                    try:
                        r=self.row(ident)
                        fields={'reason':str(e)}
                        if action=='archive' and r['status'] not in ('verified','released','restored'):
                            fields['status']='error'
                        self.update(ident,action+'-failed',**fields)
                    except Exception:
                        pass
                    results.append({'id':ident,'ok':False,'error':str(e)})
        return {'results':results,'message':f"完成 {sum(r['ok'] for r in results)}/{len(results)} 个文件"}

    def snapshot(self, query=''):
        with self.db() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM files ORDER BY updated_at DESC')]
            events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 30')]
        stats={'pending_bytes':0,'pending_count':0,'archived_bytes':0,'archived_count':0,'released_bytes':0,'released_count':0,'waiting_bytes':0,'error_count':0,'cleanup_bytes':0,'cleanup_count':0,'nas_deleted_bytes':0,'nas_deleted_count':0}
        c = self.config()
        idle_cutoff = cutoff(c['cleanup_period'])
        now = time.time()
        for r in rows:
            baseline = max(r['verified_at'] or now, r['last_accessed_at'] or 0, r['access_observed_since'] or now)
            r['cleanup_idle_days'] = max(0, int((now-baseline)/86400)) if r['object'] else 0
            r['cleanup_candidate'] = bool(c['cleanup_enabled'] and r['object'] and r['sha256']
                and r['verified_at'] and r['access_observed_since'] and not r['excluded'] and not r['nas_deleted_at']
                and r['status'] in ('verified','released','restored') and baseline < idle_cutoff)
            r['cleanup_basis'] = '根据本工具取回记录和用户登记评估；无法感知 Finder / NAS 网页等外部访问'
            if r['cleanup_candidate']:
                stats['cleanup_bytes'] += r['size']; stats['cleanup_count'] += 1
            if r['status']=='pending' and not r['excluded'] and r['eligible']:
                stats['pending_bytes']+=r['size']; stats['pending_count']+=1
            if r['nas_deleted_at']:
                stats['nas_deleted_bytes']+=r['size']; stats['nas_deleted_count']+=1
            if r['object'] and not r['nas_deleted_at']:
                stats['archived_bytes']+=r['size']; stats['archived_count']+=1
            if r['released_at'] and not os.path.lexists(r['path']):
                stats['released_bytes']+=r['allocated']; stats['released_count']+=1
            if r['status']=='verified':
                stats['waiting_bytes']+=r['size']
            if r['status']=='error' or r['reason'] and r['status'] in ('verified','released','releasing'):
                stats['error_count']+=1
        if query:
            rows=[r for r in rows if query.lower() in (' '.join(str(r[k] or '') for k in ('path','title','thread_id'))).lower()]
        return {'config':c,'files':rows,'stats':stats,'events':events}


def serve(engine, port):
    token=secrets.token_urlsafe(32)
    job={'running':False,'message':'就绪','result':None}
    guard=threading.Lock()
    asset=Path(__file__).resolve().parent.parent/'assets/dashboard.html'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def reply(self, data, status=200, content_type='application/json; charset=utf-8'):
            body=json.dumps(data,ensure_ascii=False).encode() if isinstance(data,(dict,list)) else data
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
            self.end_headers(); self.wfile.write(body)
        def authorized(self):
            host='127.0.0.1:'+str(self.server.server_port)
            return self.headers.get('Host')==host and self.headers.get('Authorization')=='Bearer '+token and self.headers.get('Origin', 'http://'+host)=='http://'+host
        def do_GET(self):
            if self.path=='/':
                return self.reply(asset.read_bytes(),content_type='text/html; charset=utf-8')
            if not self.authorized():
                return self.reply({'error':'本地访问令牌无效，请使用启动命令给出的完整链接'},403)
            if self.path=='/api/state':
                data=engine.snapshot()
                with guard:
                    data['job']=dict(job)
                return self.reply(data)
            return self.reply({'error':'Not found'},404)
        def do_POST(self):
            if not self.authorized():
                return self.reply({'error':'Forbidden'},403)
            try:
                length=int(self.headers.get('Content-Length',0))
                if length<1 or length>1024*1024:
                    raise ValueError('请求大小无效')
                data=json.loads(self.rfile.read(length))
                if self.path=='/api/config':
                    return self.reply(engine.save_config(data))
                action=self.path.removeprefix('/api/')
                if action not in ('connect','scan','archive','release','restore','exclude','include','mark-used','delete-preview','delete-confirm'):
                    return self.reply({'error':'Not found'},404)
                with guard:
                    if job['running']:
                        return self.reply({'error':'已有任务运行中'},409)
                    job.update(running=True,message='正在执行：'+action,result=None)
                def run():
                    try:
                        if action=='delete-preview':
                            result=engine.preview_delete(data.get('ids'))
                        elif action=='delete-confirm':
                            result=engine.confirm_delete(data.get('token'),data.get('phrase'))
                        else:
                            result=getattr(engine,action)() if action in ('scan','connect') else engine.batch(action,data.get('ids'))
                        with guard:
                            job.update(running=False,message=result.get('message','完成'),result=result)
                    except Exception as e:
                        with guard:
                            job.update(running=False,message=str(e),result={'error':str(e)})
                threading.Thread(target=run,daemon=True).start()
                self.reply({'started':True},202)
            except Exception as e:
                self.reply({'error':str(e)},400)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print('Dashboard: http://127.0.0.1:'+str(server.server_port)+'/#'+token,flush=True)
    server.serve_forever()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',help='独立配置与索引目录')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('serve'); p.add_argument('--port',type=int,default=0)
    sub.add_parser('scan'); sub.add_parser('status'); sub.add_parser('connect')
    p=sub.add_parser('configure'); p.add_argument('--file',required=True)
    p=sub.add_parser('find'); p.add_argument('query')
    p=sub.add_parser('delete-preview'); p.add_argument('ids',nargs='+')
    p=sub.add_parser('delete-confirm'); p.add_argument('--token',required=True); p.add_argument('--phrase',required=True)
    for command in ('archive','release','restore','exclude','include','mark-used'):
        p=sub.add_parser(command); p.add_argument('ids',nargs='+')
    args=parser.parse_args(); engine=Archive(args.state)
    if args.command=='serve':
        serve(engine,args.port); return
    if args.command=='configure':
        result=engine.save_config(json.loads(Path(args.file).read_text()))
    elif args.command=='delete-preview':
        result=engine.preview_delete(args.ids)
    elif args.command=='delete-confirm':
        result=engine.confirm_delete(args.token,args.phrase)
    elif args.command in ('status','find'):
        result=engine.snapshot(getattr(args,'query',''))
    elif args.command in ('scan','connect'):
        result=getattr(engine,args.command)()
    else:
        result=engine.batch(args.command,args.ids)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if 'results' in result and any(not r['ok'] for r in result['results']):
        raise SystemExit(1)


if __name__=='__main__':
    main()
