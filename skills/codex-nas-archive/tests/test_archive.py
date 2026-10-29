import datetime as dt
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('archive',Path(__file__).resolve().parents[1]/'scripts/archive.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve()
        self.project=self.root/'projects/old'; (self.project/'outputs').mkdir(parents=True)
        self.file=self.project/'outputs/成片.mp4';self.file.write_bytes(b'original content'*1000)
        self.old=time.time()-100*86400;os.utime(self.file,(self.old,self.old))
        self.nas=self.root/'NAS';self.nas.mkdir();self.home=self.root/'codex';self.home.mkdir()
        self.threads=self.home/'state_5.sqlite'
        with closing(sqlite3.connect(self.threads, isolation_level=None)) as c:
            c.execute('CREATE TABLE threads(id TEXT,cwd TEXT,title TEXT,updated_at INTEGER,is_pinned INTEGER)')
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('thread-old',str(self.project),'测试视频会话',self.old,0))
        self.e=m.Archive(self.root/'state',test_local=True)
        c=self.e.config();c['codex_home']=str(self.home);m.atomic_json(self.e.config_path,c)
        self.e.save_config({'roots':[str(self.root/'projects')],'period':{'value':1,'unit':'months'},'nas_path':str(self.nas),'grace_days':0})
        self.e.connect();self.e.scan();self.id=self.e.snapshot()['files'][0]['id']
    def tearDown(self):
        self.tmp.cleanup()
    def batch(self,action):
        return self.e.batch(action,[self.id])['results'][0]
    def archive(self):
        self.assertTrue(self.batch('archive')['ok'])
    def test_roundtrip_and_space_accounting(self):
        original=self.file.read_bytes();self.archive()
        self.assertEqual(self.e.snapshot()['stats']['released_bytes'],0)
        self.assertTrue(self.batch('release')['ok']);self.assertFalse(self.file.exists())
        self.assertGreater(self.e.snapshot()['stats']['released_bytes'],0)
        self.assertTrue(self.batch('restore')['ok']);self.assertEqual(self.file.read_bytes(),original)
        self.assertEqual(self.e.snapshot()['stats']['released_bytes'],0)
    def test_modified_source_not_removed(self):
        self.archive();self.file.write_bytes(b'new content');os.utime(self.file,(self.old,self.old))
        self.assertFalse(self.batch('release')['ok']);self.assertEqual(self.file.read_bytes(),b'new content')
    def test_corrupt_remote_blocks_release(self):
        self.archive();r=self.e.row(self.id);self.e.remote_object(r).write_bytes(b'corrupt')
        self.assertFalse(self.batch('release')['ok']);self.assertTrue(self.file.exists())
    def test_recent_thread_blocks_release(self):
        self.archive()
        with closing(sqlite3.connect(self.threads, isolation_level=None)) as c:c.execute('UPDATE threads SET updated_at=?',(time.time(),))
        self.assertFalse(self.batch('release')['ok']);self.assertTrue(self.file.exists())
    def test_restore_conflict_never_overwrites(self):
        self.archive();self.assertTrue(self.batch('release')['ok']);self.file.write_bytes(b'new edit')
        self.assertFalse(self.batch('restore')['ok']);self.assertEqual(self.file.read_bytes(),b'new edit')
    def test_wrong_mount_identity(self):
        self.archive();m.atomic_json(self.nas/'.codex-archive-destination.json',{'id':'different'})
        self.assertFalse(self.batch('release')['ok']);self.assertTrue(self.file.exists())
    def test_offline_preserves_local(self):
        self.archive();os.rename(self.nas,self.root/'offline')
        self.assertFalse(self.batch('release')['ok']);self.assertTrue(self.file.exists())
    def test_grace_period(self):
        self.archive();self.e.save_config({'grace_days':7})
        self.assertFalse(self.batch('release')['ok']);self.assertTrue(self.file.exists())
    def test_shared_and_pinned_are_skipped(self):
        with closing(sqlite3.connect(self.threads, isolation_level=None)) as c:c.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('other',str(self.project),'shared',self.old,0))
        self.e.scan();self.assertEqual(self.e.row(self.id)['status'],'skipped');self.assertFalse(self.batch('archive')['ok'])
        with closing(sqlite3.connect(self.threads, isolation_level=None)) as c:
            c.execute("DELETE FROM threads WHERE id='other'");c.execute('UPDATE threads SET is_pinned=1')
        self.e.scan();self.assertIn('置顶',self.e.row(self.id)['reason'])
    def test_exclude(self):
        self.batch('exclude');self.assertFalse(self.batch('archive')['ok']);self.assertEqual(self.e.snapshot()['stats']['pending_count'],0)
        self.batch('include');self.archive()
    def test_interrupted_release_recovers(self):
        self.archive();temp=self.file.with_name('.release-interrupted')
        self.e.update(self.id,'test',status='releasing',release_temp=str(temp));os.rename(self.file,temp)
        with self.e.lock():self.e.recover()
        self.assertTrue(self.file.exists());self.assertFalse(temp.exists());self.assertEqual(self.e.row(self.id)['status'],'verified')
    def test_interrupted_copy_retry(self):
        self.e.update(self.id,'test',status='copying')
        self.archive();self.assertTrue(self.file.exists())
    def test_symlink_refused(self):
        self.file.unlink();target=self.root/'target.mp4';target.write_bytes(b'precious');self.file.symlink_to(target)
        self.assertFalse(self.batch('archive')['ok']);self.assertEqual(target.read_bytes(),b'precious')
    def test_mount_guard_rejects_local_folder(self):
        self.e.test_local=False
        with patch.object(m.subprocess,'run') as run:
            run.return_value.stdout='/dev/disk on / (apfs, local)\n'
            with self.assertRaises(ValueError):self.e.destination()
    def test_mount_guard_accepts_smb(self):
        self.e.test_local=False
        with patch.object(m.subprocess,'run') as run:
            run.return_value.stdout='//user@nas/share on '+str(self.nas)+' (smbfs, nodev, nosuid)\n'
            self.assertEqual(self.e.destination(),self.nas)
    def test_natural_month(self):
        n=dt.datetime(2026,3,31,12,tzinfo=dt.timezone.utc)
        got=dt.datetime.fromtimestamp(m.cutoff({'value':1,'unit':'months'},n),dt.timezone.utc)
        self.assertEqual(got,dt.datetime(2026,2,28,12,tzinfo=dt.timezone.utc))
    def test_connection_rejects_password_url(self):
        with self.assertRaises(ValueError):self.e.save_config({'nas_url':'smb://user:password@host/share'})
    def test_find_matches_title_and_path(self):
        self.assertEqual(len(self.e.snapshot('测试视频')['files']),1)
        self.assertEqual(len(self.e.snapshot(str(self.file))['files']),1)
    def test_corrupt_nas_blocks_restore(self):
        self.archive();self.batch('release');self.e.remote_object(self.e.row(self.id)).write_bytes(b'corrupt')
        self.assertFalse(self.batch('restore')['ok']);self.assertFalse(self.file.exists())
    def test_file_change_during_copy(self):
        real=m.digest
        def modifying_digest(p):
            result=real(p)
            if '.incoming' in str(p): self.file.write_bytes(b'changed during copy')
            return result
        with patch.object(m,'digest',side_effect=modifying_digest):self.assertFalse(self.batch('archive')['ok'])
        self.assertTrue(self.file.exists());self.assertIsNone(self.e.row(self.id)['object'])

    def test_connection_and_config_do_not_skip_onboarding(self):
        self.assertFalse(self.e.config()['configured'])
        self.e.save_config({'configured':True})
        self.assertTrue(self.e.config()['configured'])
    def test_period_change_invalidates_candidates(self):
        self.e.save_config({'period':{'value':120,'unit':'days'}})
        self.assertEqual(self.e.snapshot()['stats']['pending_count'],0)
        self.assertFalse(self.batch('archive')['ok'])
    def test_duplicate_archive_is_idempotent(self):
        self.archive();obj=self.e.row(self.id)['object'];self.archive()
        self.assertEqual(self.e.row(self.id)['object'],obj)
    def test_lock_blocks_concurrent_operations(self):
        with self.e.lock():
            with self.assertRaises(ValueError):self.e.scan()
    def test_low_space_preserves_source(self):
        with patch.object(m.shutil,'disk_usage') as usage:
            usage.return_value.free=0
            self.assertFalse(self.batch('archive')['ok'])
        self.assertTrue(self.file.exists())
    def test_publish_failure_can_retry(self):
        with patch.object(m.os,'rename',side_effect=OSError('simulated disconnect')):
            self.assertFalse(self.batch('archive')['ok'])
        self.assertTrue(self.file.exists());self.archive()
    def test_http_requires_token_and_same_origin(self):
        import subprocess,sys,http.client
        from urllib.parse import urlsplit
        script=Path(__file__).resolve().parents[1]/'scripts/archive.py'
        process=subprocess.Popen([sys.executable,str(script),'--state',str(self.e.state),'serve'],stdout=subprocess.PIPE,text=True)
        try:
            address=urlsplit(process.stdout.readline().strip().split('Dashboard: ')[1])
            conn=http.client.HTTPConnection(address.hostname,address.port,timeout=3)
            conn.request('GET','/api/state');r=conn.getresponse();self.assertEqual(r.status,403);r.read()
            headers={'Authorization':'Bearer '+address.fragment}
            conn.request('GET','/api/state',headers=headers);r=conn.getresponse();self.assertEqual(r.status,200);self.assertIn('stats',json.loads(r.read()))
            headers['Origin']='https://untrusted.example'
            conn.request('POST','/api/scan',body='{}',headers=headers);r=conn.getresponse();self.assertEqual(r.status,403);r.read();conn.close()
        finally:
            process.terminate();process.wait(timeout=5);process.stdout.close()

    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):self.e.save_config({'nas_path':str(self.nas/'../projects')})

    def old_archive(self):
        self.archive()
        self.e.save_config({'cleanup_period':{'value':7,'unit':'days'}})
        t=time.time()-10*86400
        self.e.update(self.id,'test-old-archive',verified_at=t,access_observed_since=t)

    def test_cleanup_recommendation_is_read_only(self):
        self.old_archive();r=self.e.row(self.id);remote=self.e.remote_object(r)
        before=self.file.read_bytes();nas_before=remote.read_bytes()
        snap=self.e.snapshot()
        self.assertTrue(snap['files'][0]['cleanup_candidate'])
        self.assertEqual(snap['stats']['cleanup_count'],1)
        self.assertEqual(snap['stats']['released_bytes'],0)
        self.assertEqual(self.file.read_bytes(),before);self.assertEqual(remote.read_bytes(),nas_before)
        self.assertEqual(self.e.row(self.id)['last_accessed_at'],None)

    def test_cleanup_period_is_separate_and_can_be_disabled(self):
        self.old_archive()
        self.e.save_config({'cleanup_period':{'value':3,'unit':'months'}})
        self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],0)
        self.assertEqual(self.e.config()['period'],{'value':1,'unit':'months'})
        self.e.save_config({'cleanup_period':{'value':7,'unit':'days'},'cleanup_enabled':False})
        self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],0)

    def test_successful_restore_restarts_idle_timer(self):
        self.old_archive();self.batch('release')
        self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],1)
        self.assertTrue(self.batch('restore')['ok'])
        self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],0)
        self.assertEqual(self.e.row(self.id)['access_source'],'restore')

    def test_already_local_use_restarts_idle_timer(self):
        self.old_archive();self.assertTrue(self.batch('restore')['ok'])
        self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],0)

    def test_failed_restore_does_not_record_use(self):
        self.old_archive();self.file.write_bytes(b'new local work')
        self.assertFalse(self.batch('restore')['ok'])
        self.assertIsNone(self.e.row(self.id)['last_accessed_at'])

    def test_external_access_can_be_registered(self):
        self.old_archive();self.assertTrue(self.batch('mark-used')['ok'])
        self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],0)
        self.assertEqual(self.e.row(self.id)['access_source'],'manual')

    def test_never_archived_not_recommended(self):
        self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],0)
        self.assertFalse(self.batch('mark-used')['ok'])

    def test_keep_file_excluded_from_cleanup(self):
        self.old_archive();self.batch('exclude')
        self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],0)
        self.batch('include');self.assertEqual(self.e.snapshot()['stats']['cleanup_count'],1)

    def test_old_archive_migration_starts_observation_now(self):
        self.old_archive()
        with self.e.db() as db:
            for name in ('last_accessed_at','access_source','access_observed_since'):
                db.execute('ALTER TABLE files DROP COLUMN '+name)
        upgraded=m.Archive(self.e.state,test_local=True)
        self.assertGreater(upgraded.row(self.id)['access_observed_since'],time.time()-10)
        self.assertEqual(upgraded.snapshot()['stats']['cleanup_count'],0)
        self.assertEqual(upgraded.row(self.id)['sha256'],self.e.row(self.id)['sha256'])

    def test_cleanup_cannot_delete_remote_via_batch(self):
        self.old_archive();remote=self.e.remote_object(self.e.row(self.id))
        with self.assertRaises(ValueError):self.e.batch('delete-nas',[self.id])
        self.assertTrue(remote.exists())

    def delete_ticket(self):
        return self.e.preview_delete([self.id])['delete_preview']

    def test_delete_preview_never_deletes(self):
        self.archive();r=self.e.row(self.id);remote=self.e.remote_object(r)
        preview=self.delete_ticket()
        self.assertTrue(preview['items'][0]['local_copy_verified'])
        self.assertTrue(remote.exists());self.assertTrue(self.file.exists())
        self.assertEqual(self.e.row(self.id)['status'],'verified')

    def test_confirmed_delete_retains_local_and_index(self):
        self.archive();remote=self.e.remote_object(self.e.row(self.id));original=self.file.read_bytes()
        result=self.e.confirm_delete(self.delete_ticket()['token'],'删除 NAS 归档')
        self.assertTrue(result['results'][0]['ok']);self.assertFalse(remote.exists())
        self.assertEqual(self.file.read_bytes(),original)
        self.assertEqual(self.e.row(self.id)['status'],'nas-deleted')
        snap=self.e.snapshot();self.assertEqual(snap['stats']['archived_count'],0)
        self.assertEqual(snap['stats']['nas_deleted_count'],1)
        self.assertFalse(self.batch('restore')['ok'])
        manifest=json.loads((self.nas/'manifests'/(self.id+'.json')).read_text())
        self.assertEqual(manifest['status'],'nas-deleted')

    def test_delete_without_local_copy_is_explicit(self):
        self.archive();self.batch('release');preview=self.delete_ticket()
        self.assertEqual(preview['without_local_copy'],1)
        self.assertFalse(preview['items'][0]['local_copy_verified'])
        result=self.e.confirm_delete(preview['token'],'删除 NAS 归档')
        self.assertTrue(result['results'][0]['ok']);self.assertFalse(self.file.exists())
        self.assertEqual(self.e.snapshot()['stats']['released_count'],1)

    def test_delete_requires_exact_confirmation(self):
        self.archive();preview=self.delete_ticket();remote=self.e.remote_object(self.e.row(self.id))
        with self.assertRaises(ValueError):self.e.confirm_delete(preview['token'],'yes')
        self.assertTrue(remote.exists())

    def test_delete_ticket_single_use(self):
        self.archive();preview=self.delete_ticket()
        self.e.confirm_delete(preview['token'],'删除 NAS 归档')
        with self.assertRaises(ValueError):self.e.confirm_delete(preview['token'],'删除 NAS 归档')

    def test_delete_ticket_expiry(self):
        self.archive();preview=self.delete_ticket()
        with self.e.db() as db:db.execute('UPDATE delete_previews SET expires=?',(time.time()-1,))
        with self.assertRaises(ValueError):self.e.confirm_delete(preview['token'],'删除 NAS 归档')
        self.assertTrue(self.e.remote_object(self.e.row(self.id)).exists())

    def test_delete_rejects_recent_usage(self):
        self.archive();preview=self.delete_ticket();self.batch('mark-used')
        with self.assertRaises(ValueError):self.e.confirm_delete(preview['token'],'删除 NAS 归档')
        self.assertTrue(self.e.remote_object(self.e.row(self.id)).exists())

    def test_delete_rejects_changed_or_missing_backup(self):
        self.archive();preview=self.delete_ticket();self.file.unlink()
        with self.assertRaises(ValueError):self.e.confirm_delete(preview['token'],'删除 NAS 归档')
        self.assertTrue(self.e.remote_object(self.e.row(self.id)).exists())

    def test_delete_rejects_changed_nas_object(self):
        self.archive();preview=self.delete_ticket();remote=self.e.remote_object(self.e.row(self.id));remote.write_bytes(b'new important content')
        with self.assertRaises(ValueError):self.e.confirm_delete(preview['token'],'删除 NAS 归档')
        self.assertEqual(remote.read_bytes(),b'new important content')

    def test_protected_archive_cannot_preview_deletion(self):
        self.archive();self.batch('exclude')
        with self.assertRaises(ValueError):self.delete_ticket()

    def test_interrupted_delete_does_not_automatically_retry(self):
        self.archive();self.e.update(self.id,'test',status='nas-deleting',delete_previous_status='verified')
        with self.e.lock():self.e.recover()
        self.assertEqual(self.e.row(self.id)['status'],'verified')
        self.assertTrue(self.e.remote_object(self.e.row(self.id)).exists())

    def test_interrupted_completed_delete_reconciles(self):
        self.archive();remote=self.e.remote_object(self.e.row(self.id))
        self.e.update(self.id,'test',status='nas-deleting',delete_previous_status='verified');remote.unlink()
        with self.e.lock():self.e.recover()
        self.assertEqual(self.e.row(self.id)['status'],'nas-deleted')
        self.assertFalse(self.batch('restore')['ok'])

    def test_scan_preserves_deletion_record(self):
        self.archive();self.e.confirm_delete(self.delete_ticket()['token'],'删除 NAS 归档')
        self.e.scan();self.assertEqual(self.e.row(self.id)['status'],'nas-deleted')
        self.assertEqual(self.e.snapshot()['stats']['archived_count'],0)

    def test_can_rearchive_local_file_after_remote_deletion(self):
        self.archive();self.e.confirm_delete(self.delete_ticket()['token'],'删除 NAS 归档')
        self.archive();self.assertIsNone(self.e.row(self.id)['nas_deleted_at'])
        self.assertEqual(self.e.snapshot()['stats']['archived_count'],1)

if __name__=='__main__':unittest.main(verbosity=2)
