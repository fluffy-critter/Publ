# index.py
''' Content indexer '''

import concurrent.futures
import logging
import os
import threading
import time
import typing

import watchdog.events
import watchdog.observers
from flask import current_app
from pony import orm

from . import category, entry, model, utils

LOGGER = logging.getLogger(__name__)

ENTRY_TYPES = ['.md', '.htm', '.html']
CATEGORY_TYPES = ['.cat', '.meta']


class Indexer:
    """ Class which handles the scheduling of file indexing """
    # pylint:disable=too-few-public-methods
    QUEUE_ITEM = typing.Tuple[str, typing.Optional[str], bool]

    def __init__(self, wait_time: float):
        self.thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="Indexer")
        self._pending: typing.Set[Indexer.QUEUE_ITEM] = set()
        self._lock = threading.Lock()
        self._running: typing.Optional[concurrent.futures.Future] = None
        self._wait_time = wait_time

    def scan_file(self, fullpath: str, relpath: typing.Optional[str], fixups: bool):
        """ Scan a file for the index

        fullpath -- The full path to the file
        relpath -- The path to the file, relative to its base directory
        fixups -- Whether to perform fixup routines on the file

        This calls into various modules' scanner functions; the expectation is that
        the scan_file function will return a truthy value if it was scanned
        successfully, False if it failed, and None if there is nothing to scan.
        """

        with self._lock:
            self._pending.add((fullpath, relpath, fixups))
        self._schedule(self._wait_time)

    def _schedule(self, wait: float = 0):
        with self._lock:
            if not self._running or self._running.done():
                if wait:
                    # busywait the worker thread to wait for things to settle down
                    # (and to batch up a bunch of updates in a row)
                    self.thread_pool.submit(time.sleep, wait)

                # run the actual pending task
                self._running = self.thread_pool.submit(self._scan_pending)

                LOGGER.debug("worker started %s", self._running)

    def _scan_pending(self):
        """ Scan all the pending items """
        try:
            with self._lock:
                items = self._pending
                self._pending = set()
            LOGGER.debug("Processing %d files", len(items))

            # process the known items
            for item in items:
                self._scan_file(*item)

            # and then schedule a catchup for anything that happened
            # while this scan was happening
            if items:
                self.thread_pool.submit(self._schedule)

        except Exception:  # pylint:disable=broad-except
            LOGGER.exception("_scan_pending failed")

    def _scan_file(self, fullpath: str, relpath: typing.Optional[str], fixups: bool):
        LOGGER.debug("Scanning file: %s (%s) %s", fullpath, relpath, fixups)

        def do_scan() -> typing.Optional[bool]:
            """ helper function to do the scan and gather the result """
            _, ext = os.path.splitext(fullpath)

            try:
                if ext in ENTRY_TYPES:
                    LOGGER.info("Scanning entry: %s", fullpath)
                    return entry.scan_file(fullpath, relpath, fixups)

                if ext in CATEGORY_TYPES:
                    LOGGER.info("Scanning meta info: %s", fullpath)
                    return category.scan_file(fullpath, relpath)

                return None
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Got error parsing %s", fullpath)
                return False

        result = do_scan()
        if result is False and not fixups:
            LOGGER.info("Scheduling fixup for %s", fullpath)
            self.scan_file(fullpath, relpath, True)
        else:
            LOGGER.debug("%s complete", fullpath)
            set_fingerprint(fullpath)
        return result


@orm.db_session
def last_modified() -> typing.Tuple[typing.Optional[str],
                                    typing.Optional[int],
                                    typing.Optional[str]]:
    """ information about the most recently modified file, for cache-busting
    purposes """
    files = model.FileFingerprint.select().order_by(
        orm.desc(model.FileFingerprint.file_mtime))
    for file in files:
        return file.file_path, file.file_mtime, file.fingerprint
    return None, None, None


def _work_queue():
    return getattr(current_app.indexer.thread_pool, '_work_queue', None)


def queue_length() -> typing.Optional[int]:
    """ Return the approximate length of the work queue """
    work_queue = _work_queue()
    return work_queue.qsize() if work_queue else None


def in_progress() -> bool:
    """ Return if there's an index in progress """
    return bool(queue_length)


def is_scannable(fullpath) -> bool:
    """ Determine if a file needs to be scanned """
    _, ext = os.path.splitext(fullpath)
    return ext in ENTRY_TYPES or ext in CATEGORY_TYPES


@orm.db_session
def get_last_fingerprint(fullpath) -> typing.Optional[str]:
    """ Get the last known fingerprint for a file """
    record = model.FileFingerprint.get(file_path=fullpath)
    if record:
        return record.fingerprint
    return None


@orm.db_session(retry=5)
def set_fingerprint(fullpath, fingerprint=None):
    """ Set the last known modification time for a file """
    try:
        fingerprint = fingerprint or utils.file_fingerprint(fullpath)

        record = model.FileFingerprint.get(file_path=fullpath)
        if record and record.fingerprint != fingerprint:
            record.set(fingerprint=fingerprint,
                       file_mtime=os.stat(fullpath).st_mtime)
        else:
            record = model.FileFingerprint(
                file_path=fullpath,
                fingerprint=fingerprint,
                file_mtime=os.stat(fullpath).st_mtime)
        orm.commit()
    except FileNotFoundError:
        orm.delete(fp for fp in model.FileFingerprint if fp.file_path == fullpath)


class IndexWatchdog(watchdog.events.PatternMatchingEventHandler):
    """ Watchdog handler """

    def __init__(self, indexer, content_dir):
        super().__init__(ignore_directories=True)
        self._indexer = indexer
        self.content_dir = content_dir

    def update_file(self, fullpath):
        """ Update a file """
        LOGGER.debug("Scheduling reindex of %s", fullpath)
        relpath = os.path.relpath(fullpath, self.content_dir)
        self._indexer.scan_file(fullpath, relpath, False)

    def on_created(self, event):
        """ on_created handler """
        LOGGER.debug("file created: %s", event.src_path)
        if not event.is_directory:
            self.update_file(event.src_path)

    def on_modified(self, event):
        """ on_modified handler """
        LOGGER.debug("file modified: %s", event.src_path)
        if not event.is_directory:
            self.update_file(event.src_path)

    def on_moved(self, event):
        """ on_moved handler """
        LOGGER.debug("file moved: %s -> %s", event.src_path, event.dest_path)
        if not event.is_directory:
            self.update_file(event.src_path)
            self.update_file(event.dest_path)

    def on_deleted(self, event):
        """ on_deleted handler """
        LOGGER.debug("File deleted: %s", event.src_path)
        if not event.is_directory:
            self.update_file(event.src_path)


def background_scan(content_dir):
    """ Start background scanning a directory for changes """
    observer = watchdog.observers.Observer()
    observer.schedule(IndexWatchdog(current_app.indexer, content_dir),
                      content_dir, recursive=True)
    logging.info("Watching %s for changes", content_dir)
    observer.start()


def prune_missing(table):
    """ Prune any files which are missing from the specified table """
    LOGGER.debug("Pruning missing %s files", table.__name__)
    removed_paths: typing.List[str] = []

    @orm.db_session(retry=5)
    def fill():
        try:
            for item in table.select():
                if not os.path.isfile(item.file_path):
                    LOGGER.info("%s disappeared: %s", table.__name__, item.file_path)
                    removed_paths.append(item.file_path)
        except Exception:  # pylint:disable=broad-except
            LOGGER.exception("Error pruning %s", table.__name__)

    @orm.db_session(retry=5)
    def kill(path):
        LOGGER.debug("Pruning %s %s", table.__name__, path)
        try:
            item = table.get(file_path=path)
            if item and not os.path.isfile(item.file_path):
                item.delete()
        except Exception:  # pylint:disable=broad-except
            LOGGER.exception("Error pruning %s", table.__name__)

    fill()
    for item in removed_paths:
        kill(item)


def scan_index(content_dir):
    """ Scan all files in a content directory """
    LOGGER.debug("Reindexing content from %s", content_dir)

    indexer = current_app.indexer

    def scan_directory(root, files):
        """ Helper function to scan a single directory """
        LOGGER.debug("scanning directory %s", root)
        try:
            for file in files:
                fullpath = os.path.join(root, file)
                relpath = os.path.relpath(fullpath, content_dir)

                if not is_scannable(fullpath):
                    continue

                fingerprint = utils.file_fingerprint(fullpath)
                last_fingerprint = get_last_fingerprint(fullpath)
                if fingerprint != last_fingerprint:
                    LOGGER.debug("%s: %s -> %s", fullpath, last_fingerprint, fingerprint)
                    indexer.scan_file(fullpath, relpath, False)
        except Exception:  # pylint:disable=broad-except
            LOGGER.exception("Got error parsing directory %s", root)

    for root, _, files in os.walk(content_dir, followlinks=True):
        indexer.thread_pool.submit(scan_directory, root, files)

    for table in (model.Entry, model.Category, model.Image, model.FileFingerprint):
        indexer.thread_pool.submit(prune_missing, table)
