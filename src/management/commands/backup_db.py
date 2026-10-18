import gzip
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = ("Make a verified, compressed copy of the SQLite database. "
            "Safe to run while the site is in use; schedule it daily.")

    def add_arguments(self, parser):
        parser.add_argument('--label', default='',
                            help="Added to the file name, e.g. 'pre-deploy'.")

    def handle(self, *args, **options):
        if connection.vendor != 'sqlite':
            raise CommandError('backup_db only supports SQLite.')

        source_path = Path(settings.DATABASES['default']['NAME'])
        backup_dir = Path(settings.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        label = f"-{options['label']}" if options['label'] else ''
        raw_path = backup_dir / f'db-{stamp}{label}.sqlite3'
        gz_path = raw_path.with_name(raw_path.name + '.gz')

        # The online backup API copies a consistent snapshot even if another
        # process writes to the database meanwhile.
        source = sqlite3.connect(f'file:{source_path}?mode=ro', uri=True)
        target = sqlite3.connect(raw_path)
        try:
            source.backup(target)
            check = target.execute('PRAGMA integrity_check').fetchone()[0]
            results = target.execute('SELECT count(*) FROM src_studentresult').fetchone()[0]
            students = target.execute('SELECT count(*) FROM src_student').fetchone()[0]
        finally:
            target.close()
            source.close()

        if check != 'ok':
            raw_path.unlink(missing_ok=True)
            raise CommandError(f'Backup failed integrity check: {check}')

        with open(raw_path, 'rb') as src, gzip.open(gz_path, 'wb') as dst:
            shutil.copyfileobj(src, dst)
        raw_path.unlink()

        self.stdout.write(self.style.SUCCESS(
            f'Backup written: {gz_path} ({students} students, {results} results)'))

        self._prune(backup_dir, settings.BACKUP_KEEP)

    def _prune(self, backup_dir, keep):
        # Labelled backups (e.g. pre-deploy) are kept; only routine ones rotate.
        routine = sorted(p for p in backup_dir.glob('db-*.sqlite3.gz')
                         if p.name.count('-') == 2)
        for old in routine[:-keep] if keep > 0 else []:
            old.unlink()
            self.stdout.write(f'Removed old backup: {old.name}')
