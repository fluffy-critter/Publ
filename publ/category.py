# category.py
""" The Category object passed to entry and category views """

from __future__ import absolute_import

import os

from flask import url_for

from . import model
from . import utils


class Category:
    """ Wrapper for category information """
    # pylint: disable=too-few-public-methods

    def __init__(self, path):
        """ Initialize a category wrapper

        path -- the path to the category
        """

        self.path = path
        self.basename = os.path.basename(path)

        self._parent = os.path.dirname(path) if path else None

        subcat_query = model.Entry.select(model.Entry.category).distinct()
        if path:
            subcat_query = subcat_query.where(
                model.Entry.category.startswith(path + '/'))
        else:
            subcat_query = subcat_query.where(model.Entry.category != path)

        self._subcats_recursive = subcat_query.order_by(model.Entry.category)

        self.link = utils.CallableProxy(self._link)

        self.subcats = utils.CallableProxy(self._get_subcats)

    def _link(self, template='', absolute=False):
        return url_for('category',
                       category=self.path,
                       template=template,
                       _external=absolute)

    def __str__(self):
        return self.path

    def __getattr__(self, name):
        """ Lazily bind related objects """
        # pylint: disable=attribute-defined-outside-init
        if name == 'parent':
            self.parent = Category(self._parent) if (
                self._parent != None) else None
            return self.parent

        raise AttributeError("Unknown category attribute {}".format(name))

    def _get_subcats(self, recurse=False):
        """ Get the subcategories of this category

        recurse -- whether to include their subcategories as well
        """

        if recurse:
            # No need to filter
            return [Category(e.category) for e in self._subcats_recursive]

        # get all the subcategories, with only the first subdir added

        # number of path components to ingest
        parts = len(self.path.split('/')) + 1 if self.path else 1

        # get the subcategories
        subcats = [e.category for e in self._subcats_recursive]

        # convert them into separated pathlists with only 'parts' parts
        subcats = [c.split('/')[:parts] for c in subcats]

        # join them back into a path, and make unique
        subcats = {'/'.join(c) for c in subcats}

        # convert to a bunch of Category objects and bind to the Category
        return [Category(c) for c in sorted(subcats)]
