# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""View for showing diffs between mercurial revisions.

The revisions don't necessarily need to be in the same repository, as long
as the repositories are related.
"""
from __future__ import absolute_import

from difflib import SequenceMatcher

from django.shortcuts import render
from django import http
from django.views.generic.base import View

from life.models import Repository, Changeset

import hglib

from compare_locales.parser import getParser, FluentEntity
from compare_locales.compare import AddRemove, Tree as DataTree


class BadRevision(Exception):
    "Revision could not be resolved"
    pass


class constdict(dict):
    '''Subclass dict to not allow modifications'''

    def __setitem__(self, key, value):
        raise NotImplementedError


class DiffView(View):
    # empty class default for tests
    # overwrite with mutable instance members if you need non-empty values
    moved = copied = constdict()
    rev1 = rev2 = None

    def _universal_newlines(self, content):
        "CompareLocales reads files with universal newlines, fake that"
        return content.replace('\r\n', '\n').replace('\r', '\n')

    def get(self, request):
        '''Handle GET requests'''
        # The code validates the input, opens up an hglib client in a
        # context, and then goes through .status() and .cat() to
        # create a diff.
        if not request.GET.get('repo'):
            return http.HttpResponseBadRequest("Missing 'repo' parameter")
        reponame = request.GET['repo']
        try:
            self.getrepo(reponame)
        except Repository.DoesNotExist:
            raise http.Http404("Repository not found")
        if not request.GET.get('from'):
            return http.HttpResponseBadRequest("Missing 'from' parameter")
        if not request.GET.get('to'):
            return http.HttpResponseBadRequest("Missing 'to' parameter")
        # make sure we have the client open, and close it when done.
        with self.client:
            try:
                paths = self.paths4revs(request.GET['from'],
                                        request.GET['to'])
            except BadRevision as e:
                return http.HttpResponseBadRequest(e.args[0])
            diffs = DataTree(dict)
            for path, action in paths:
                lines = self.diffLines(path, action)
                v = {'path': path,
                     'renamed': self.moved.get(path),
                     'copied': self.copied.get(path)}
                if lines is None:
                    v.update({
                        'isFile': True,
                        'class': action,
                        'rev': ((action == 'removed') and request.GET['from']
                                or request.GET['to'])
                    })
                else:
                    container_class = lines and 'file' or 'empty-diff'
                    v.update({
                        'class': container_class,
                        'lines': lines
                    })
                diffs[path].update(v)
        diffs = self.tree_data(diffs)
        return render(request, 'pushes/diff.html', {
                        'given_title': request.GET.get('title', None),
                        'repo': reponame,
                        'repo_url': self.repo.url,
                        'old_rev': request.GET['from'],
                        'new_rev': request.GET['to'],
                        'diffs': diffs
                      })

    def getrepo(self, reponame):
        '''Set elmo db object and hglib client for given repo name'''
        self.repo = Repository.objects.get(name=reponame)
        self.client = hglib.open(self.repo.local_path())

    def paths4revs(self, _from, _to):
        '''Validate that the passed in revisions are valid, and computes
        the affected paths and their status.
        '''
        try:
            self.rev1 = self.real_rev(_from)
        except KeyError:
            raise BadRevision("Unrecognized 'from' parameter")
        try:
            self.rev2 = self.real_rev(_to)
        except KeyError:
            raise BadRevision("Unrecognized 'to' parameter")
        changed = []
        added = []
        removed = []
        copies = {}
        for code, path in self.client.status(rev=[self.rev1, self.rev2],
                                             copies=True):
            if code == 'M':
                changed.append(path)
            elif code == 'A':
                added.append(path)
            elif code == ' ':
                # last added file was copied
                copies[added[-1]] = path
            elif code == 'R':
                removed.append(path)
            else:
                raise RuntimeError('status code %s unexpected for %s' %
                                   (code, path))

        # split up the copies info into thos that were renames and those that
        # were copied.
        self.moved = {}
        self.copied = {}
        for new_name, old_name in copies.items():
            if old_name in removed:
                self.moved[new_name] = old_name
            else:
                self.copied[new_name] = old_name

        paths = ([(f, 'changed') for f in changed]
                 + [(f, 'removed') for f in removed
                    if f not in self.moved.values()]
                 + [(f,
                     (f in self.moved and 'moved') or
                     (f in self.copied and 'copied')
                     or 'added') for f in added])
        return paths

    def real_rev(self, rev):
        '''Validate that the given revision exists in our unified repo.
        Resolve 'default' and 'tip' if passed to the latest 'default'
        changeset in our given db repo.
        '''
        # if we get 'default' or 'tip' as revision, retrieve that
        # from the db, so that we don't rely on our local clones
        # having the same data as upstream for unified repos
        if rev in ('default', 'tip'):
            rev = (Changeset.objects
                   .filter(repositories=self.repo)
                   .filter(branch=1)  # default branch
                   .order_by('-pk')
                   .values_list('revision', flat=True)[0])
        # Convert the 'from' and 'to' to strings (instead of unicode)
        # in case mercurial needs to look for the key in binary data.
        # This prevents UnicodeWarning messages.
        ctx = self.client[str(rev)]
        return ctx.node()

    def diffLines(self, path, action):
        '''The actual l10n-aware diff for a particular file.'''
        lines = []
        try:
            p = getParser(path)
        except UserWarning:
            return None
        if action == 'added':
            a_entities = []
            a_map = {}
        else:
            realpath = (action == 'moved' and self.moved[path] or
                        action == 'copied' and self.copied[path] or
                        path)
            content = self.content(realpath, self.rev1)
            try:
                p.readContents(content)
                a_entities, a_map = p.parse()
            except:
                # consider doing something like:
                # logging.warn('Unable to parse %s', path, exc_info=True)
                return None

        if action == 'removed':
            c_entities, c_map = [], {}
        else:
            content = self.content(path, self.rev2)
            try:
                p.readContents(content)
                c_entities, c_map = p.parse()
            except:
                # consider doing something like:
                # logging.warn('Unable to parse %s', path, exc_info=True)
                return None
        ar = AddRemove()
        ar.set_left(e.key for e in a_entities)
        ar.set_right(e.key for e in c_entities)
        for action, entity in ar:
            if action == 'delete':
                lines.append({
                  'class': 'removed',
                  'oldval': [{'value': a_entities[a_map[entity]].val}],
                  'newval': '',
                  'entity': entity
                })
            elif action == 'add':
                lines.append({
                  'class': 'added',
                  'oldval': '',
                  'newval': [{'value': c_entities[c_map[entity]].val}],
                  'entity': entity
                })
            else:
                old_entity = a_entities[a_map[entity]]
                new_entity = c_entities[c_map[entity]]
                if old_entity.val != new_entity.val:
                    oldhtml, newhtml = \
                        self.diff_strings(old_entity.val, new_entity.val)
                    lines.append({'class': 'changed',
                                  'oldval': oldhtml,
                                  'newval': newhtml,
                                  'entity': entity})
                if isinstance(old_entity, FluentEntity):
                    # we're in FTL, compare attributes
                    # "same, same, but different" to entities
                    old_attrs = list(old_entity.attributes)
                    old_attr_map = dict(
                        (attr.key, i) for i, attr in enumerate(old_attrs))
                    new_attrs = list(new_entity.attributes)
                    new_attr_map = dict(
                        (attr.key, i) for i, attr in enumerate(new_attrs))
                    attr_ar = AddRemove()
                    attr_ar.set_left(attr.key for attr in old_attrs)
                    attr_ar.set_right(attr.key for attr in new_attrs)
                    for action, attr_name in attr_ar:
                        if action == 'delete':
                            lines.append({
                              'class': 'removed',
                              'oldval': [
                                  {'value':
                                   old_attrs[old_attr_map[attr_name]].val}],
                              'newval': '',
                              'entity': entity + '.' + attr_name
                            })
                        elif action == 'add':
                            lines.append({
                              'class': 'added',
                              'oldval': '',
                              'newval': [
                                  {'value':
                                   new_attrs[new_attr_map[attr_name]].val}],
                              'entity': entity + '.' + attr_name
                            })
                        else:
                            old_val = old_attrs[old_attr_map[attr_name]].val
                            new_val = new_attrs[new_attr_map[attr_name]].val
                            if old_val != new_val:
                                oldhtml, newhtml = \
                                    self.diff_strings(old_val, new_val)
                                lines.append({'class': 'changed',
                                              'oldval': oldhtml,
                                              'newval': newhtml,
                                              'entity':
                                                  entity + '.' +
                                                  attr_name})

        return lines

    def content(self, path, rev):
        content = self.client.cat([self.client.pathto(path)], rev=rev)
        content = self._universal_newlines(content)
        return content

    def diff_strings(self, oldval, newval):
        sm = SequenceMatcher(None, oldval, newval)
        oldhtml = []
        newhtml = []
        for op, o1, o2, n1, n2 in sm.get_opcodes():
            if o1 != o2:
                oldhtml.append({'class': op, 'value': oldval[o1:o2]})
            if n1 != n2:
                newhtml.append({'class': op, 'value': newval[n1:n2]})
        return oldhtml, newhtml

    def tree_data(self, tree):
        nodes = []
        for segs, subtree in sorted(tree.branches.items()):
            path = '/'.join(segs)
            if subtree.value:
                node = {
                    'children': [],
                    'value': subtree.value
                }
                nodes.append((path, node))
            else:
                nodes.append((path, {'children': self.tree_data(subtree)}))
        return nodes
