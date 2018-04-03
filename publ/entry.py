# item.py
# Functions for handling content items

import markdown
import os
import re
import arrow
import email
import uuid
import tempfile
import flask

import config

from . import model
from . import path_alias
from . import utils

class MarkdownText(utils.SelfStrCall):
    def __init__(self, text):
        self._text = text

    def __call__(self):
        # TODO instance parser with image rendition support with args specified here
        return markdown.markdown(self._text)

''' Link for an entry; defaults to an individual page '''
class EntryLink(utils.SelfStrCall):
    def __init__(self, record):
        self._record = record

    def __call__(self):
        # TODO add arguments for category/view, shortlink, etc.
        if self._record.redirect_url:
            return self._record.redirect_url

        return flask.url_for('entry',
            category=self._record.category,
            entry_id=self._record.id,
            slug_text=self._record.slug_text)

class Entry:
    def __init__(self, record):
        self._record = record   # index record
        self._message = None    # actual message payload, lazy-loaded

        # permalink is always local (ignoring redirections)
        # (although it's not very 'permanent' is it?)
        self.permalink = flask.url_for('entry',
            category=record.category,
            entry_id=record.id,
            slug_text=record.slug_text)

    ''' Ensure the message payload is loaded '''
    def _load(self):
        if not self._message:
            filepath = self._record.file_path
            with open(filepath, 'r') as file:
                self._message = email.message_from_file(file)

            body, _, more = self._message.get_payload().partition('\n~~~~~\n')

            _,ext = os.path.splitext(filepath)
            if ext == '.md':
                self.body = body and MarkdownText(body) or None
                self.more = more and MarkdownText(more) or None
            else:
                self.body = body and body or None
                self.more = more and more or None

            self.link = EntryLink(self._record)

            return True
        return False

    ''' attribute getter, to convert attributes to index and payload lookups '''
    def __getattr__(self, name):
        if hasattr(self._record, name):
            return getattr(self._record, name)

        if self._load():
            # We just loaded which modifies our own attrs, so rerun the default logic
            return getattr(self, name)
        return self._message.get(name)

    ''' Get a single header on an entry '''
    def get(self, name):
        self._load()
        return self._message.get(name)

    ''' Get all related headers on an entry, as an iterable list '''
    def get_all(self, name):
        self._load()
        return self._message.get_all(name) or []

''' convert a title into a URL-friendly slug '''
def make_slug(title):
    # TODO this should probably handle things other than English ASCII...
    return re.sub(r"[^a-zA-Z0-9]+", r"-", title.strip())

''' Attempt to guess the title from the filename '''
def guess_title(basename):
    base,_ = os.path.splitext(basename)
    return re.sub(r'[ _-]+', r' ', base).title()

def scan_file(fullpath, relpath, assign_id):
    ''' scan a file and put it into the index '''
    with open(fullpath, 'r') as file:
        entry = email.message_from_file(file)

    entry_id = entry['Entry-ID']
    if entry_id == None and not assign_id:
        return False

    fixup_needed = entry_id == None or not 'Date' in entry or not 'UUID' in entry

    basename = os.path.basename(relpath)
    title = entry['title'] or guess_title(basename)

    values = {
        'file_path': fullpath,
        'category': entry.get('Category', os.path.dirname(relpath)),
        'status': model.PublishStatus[entry.get('Status', 'SCHEDULED').upper()],
        'entry_type': model.EntryType[entry.get('Type', 'ENTRY').upper()],
        'slug_text': make_slug(entry['Slug-Text'] or title),
        'redirect_url': entry['Redirect-To'],
        'title': title,
    }

    if 'Date' in entry:
        entry_date = arrow.get(entry['Date'])
    else:
        entry_date = arrow.get(os.stat(fullpath).st_ctime).to(config.timezone)
        entry['Date'] = entry_date.format()
    values['entry_date'] = entry_date.datetime

    if entry_id != None:
        record, created = model.Entry.get_or_create(id=entry_id, defaults=values)
    else:
        record, created = model.Entry.get_or_create(file_path=fullpath, defaults=values)

    if not created:
        record.update(**values).where(model.Entry.id == record.id).execute()

    # Update the entry ID
    del entry['Entry-ID']
    entry['Entry-ID'] = str(record.id)

    if not 'UUID' in entry:
        entry['UUID'] = str(uuid.uuid4())

    # add other relationships to the index
    for alias in entry.get_all('Path-Alias', []):
        path_alias.set_alias(alias, entry=record)

    if fixup_needed:
        with tempfile.NamedTemporaryFile('w', delete=False) as file:
            tmpfile = file.name
            file.write(str(entry))
        os.replace(tmpfile, fullpath)

    return record
