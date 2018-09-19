# path_alias.py
""" Handling for URL aliases """

from __future__ import absolute_import, with_statement

from flask import url_for, redirect, current_app
from pony import orm

from . import model


@orm.db_session
def set_alias(alias, **kwargs):
    """ Set a path alias.

    Arguments:

    alias -- The alias specification
    entry -- The entry to alias it to
    category -- The category to alias it to
    url -- The external URL to alias it to

    """

    spec = alias.split()
    path = spec[0]

    values = {**kwargs, 'path': path}

    if len(spec) > 1:
        values['template'] = spec[1]

    pa = model.PathAlias.get(path=path)
    if pa:
        for k, v in values.items():
            pa.__setattr__(k, v)
    else:
        pa = model.PathAlias(**values)
    orm.commit()


@orm.db_session
def remove_alias(path):
    """ Remove a path alias.

    Arguments:

    path -- the path to remove the alias of
    """
    orm.delete(p for p in model.PathAlias if p.path == path)
    orm.commit()


def get_alias(path):
    """ Get a path alias for a single path

    Returns a tuple of (url,is_permanent)
    """
    # pylint:disable=too-many-return-statements

    record = model.PathAlias.get(path=path)

    if not record:
        return None, None

    template = record.template if record.template != 'index' else None

    if record.entry:
        if record.template:
            # a template was requested, so we go to the category page
            category = (record.category.category
                        if record.category else record.entry.category)
            return url_for('category',
                           start=record.entry.id,
                           template=template,
                           category=category), True

        from . import entry  # pylint:disable=cyclic-import
        outbound = entry.Entry(record.entry).get('Redirect-To')
        if outbound:
            # The entry has a Redirect-To (soft redirect) header
            return outbound, False

        return url_for('entry',
                       entry_id=record.entry.id,
                       category=record.entry.category,
                       slug_text=record.entry.slug_text), True

    if record.category:
        return url_for('category',
                       category=record.category.category,
                       template=template), True

    if record.url:
        # This is an outbound URL that might be changed by the user, so
        # we don't do a 301 Permanently moved
        return record.url, False

    return None, None


def get_redirect(paths):
    """ Get a redirect from a path or list of paths

    Arguments:

    paths -- either a single path string, or a list of paths to check

    Returns: a flask.redirect() result
    """

    if isinstance(paths, str):
        paths = [paths]

    for path in paths:
        url, permanent = get_alias(path)
        if url:
            return redirect(url, 301 if permanent else 302)

        url, permanent = current_app.get_path_regex(path)
        if url:
            return redirect(url, 301 if permanent else 302)

    return None
