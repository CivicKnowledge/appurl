# Copyright (c) 2017 Civic Knowledge. This file is licensed under the terms of the
# MIT, included in this distribution as LICENSE

""" """

import functools
import hashlib
import os.path
from os.path import abspath, join, dirname, basename
import time
from genericpath import exists
from urllib.parse import urlparse
from urllib.request import urlopen

from fs.errors import DirectoryExpected, NoSysPath, ResourceInvalid, DirectoryExists
from requests import HTTPError
from requests.exceptions import SSLError
from six import string_types

from appurl.util import parse_url_to_dict, copy_file_or_flo
from appurl.url import Url
from appurl.exc import *


class _NoOpFileLock(object):
    """No Op for pyfilesystem caches where locking wont work"""

    def __init__(self, lf):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            raise exc_val

    def acquire(self):
        pass

    def release(self):
        pass


class Resource(object):

    cache_path = None
    sys_path = None
    download_time = None

    def __init__(self):
        super().__init__()

    def __str__(self):
        return str(self.__dict__)


class Downloader(object):
    """Downloader objects handle downloading resrouces from the web, including authorization,
    and storing the downloaded object in a cache. Since they are the primary interface to the file cache,
    all Urls object have a link to a Downloader """

    def __init__(self, cache=None, account_accessor=None, logger=None,
                 working_dir='', callback=None):
        """
        Download and cache files, via HTTP and FTP, with retry and decompression.

        :param self:
        :param cache: A PyFs filesystem object for caching files
        :param account_accessor: An objevct for acessing account credentials. Not currently used.
        :param logger: Logging object to write debug logs to
        :param working_dir:
        :param callback: Call back to call with progress reports during downloads.
        :return:
        """

        self._cache = cache
        self.account_acessor = account_accessor
        self.logger = logger
        self.working_dir = working_dir
        self.callback = callback
        self.clean = False

    @property
    def cache(self):
        if not self._cache:
            from appurl import get_cache
            #qn = self.__module__+'.'+self.__class__.__qualname__
            self._cache = get_cache()


        return self._cache

    def get_resource(url):
        pass

    def download(self, url):


        working_dir = self.working_dir if self.working_dir else ''

        r = Resource()

        # For local files, don't download, just reference in place.
        if url.scheme == 'file':
            r.cache_path = Url(url.resource_url).path
            r.download_time = None

            # Many places the file may exist
            locations = {  # What a mess ...
                abspath(r.cache_path),
                abspath(r.cache_path.lstrip('/')),
                abspath(join(working_dir, r.cache_path)),
                abspath(r.cache_path.lstrip('/'))
            }

            for l in locations:
                if exists(l):
                    r.sys_path = l
                    break
            else:
                raise DownloadError(("File resource does not exist. Found none of:"
                                     "\n{}\n\nWorking dir = {}\ncache_path={}\nspec_path={}")
                                    .format('\n'.join(locations), working_dir, r.cache_path, url.path))

        else:
            # Not a local file, so actually need to download it.
            try:
                r.cache_path, r.download_time = self._download_with_lock(url.resource_url)
            except AccessError as e:
                # Try again, using a URL that we may have configured an account for. This is
                # primarily S3 urls, with Boto or AWS credential
                try:
                    r.cache_path, r.download_time = self._download_with_lock(url.auth_resource_url)
                except AttributeError:
                    raise e

            r.sys_path = self.cache.getsyspath(r.cache_path)

        return r

    def _download_with_lock(self, url):
        """
        Download a URL and store it in the cache.

        :param url:
        :param cache_fs:
        :param account_accessor: callable of one argument (url) returning dict with credentials.
        :param clean: Remove files from cache and re-download
        :param logger:
        :param callback:
        :return:
        """

        assert isinstance(url, string_types)

        url = url.replace('\\', '/')

        # .decode('utf8'). The fs modulegets upset when given strings, so
        # we need to decode to unicode. UTF8 is a WAG.
        try:
            parsed = urlparse(url.decode('utf8'))
        except AttributeError:
            parsed = urlparse(url)

        # Create a name for the file in the cache, based on the URL
        # the '\' replacement is because pyfs only wants to use UNIX path seperators, but
        # python os.path.join will use the one specified for the operating system.
        cache_path = join(parsed.netloc, parsed.path.strip('/'))

        # If there is a query, hash it and add it to the path
        if parsed.query:
            hash = hashlib.sha224(parsed.query.encode('utf8')).hexdigest()
            # We put the hash before the last path element, because that's the target faile, which gets
            # used to figure out what the target format should be.
            cache_path = join(dirname(cache_path), hash, basename(cache_path))

        if not self.cache.exists(cache_path):

            cache_dir = os.path.dirname(cache_path)

            try:
                self.cache.makedirs(cache_dir, recreate=True)
            except DirectoryExpected as e:

                # Probably b/c the dir name is already a file
                dn = os.path.dirname(cache_path)
                bn = os.path.basename(cache_path)
                for i in range(10):
                    try:
                        cache_path = join(dn + str(i), bn)
                        self.cache.makedirs(os.path.dirname(cache_path))
                        break
                    except DirectoryExpected as e2:
                        continue
                    except DirectoryExists:
                        pass  # ? No idea what's supposed to happen here.
                    raise e
                else:
                    raise e


        try:
            from filelock import FileLock
            lock = FileLock(self.cache.getsyspath(cache_path + '.lock'))

        except NoSysPath:
            # mem: caches, and others, don't have sys paths.
            # FIXME should check for MP operation and raise if there would be
            # contention. Mem  caches are only for testing with single processes
            lock = _NoOpFileLock()

        with lock:
            if self.cache.exists(cache_path):
                if self.clean:
                    try:
                        self.cache.remove(cache_path)
                    except ResourceInvalid:
                        pass  # Well, we tried.
                else:
                    return cache_path, None

            try:
                self._download(url, cache_path)

                return cache_path, time.time()

            except HTTPError as e:
                if e.response.status_code == 403:
                    raise AccessError("Access error on download: {}".format(e))
                else:
                    raise DownloadError("Failed to download: {}".format(e))

            except (KeyboardInterrupt, Exception):
                # This is really important -- its really bad to have partly downloaded
                # files being confused with fully downloaded ones.
                # FIXME. Should also handle signals. deleting partly downloaded files is important.
                # Maybe should have a sentinel file, or download to another name and move the
                # file after done.
                if self.cache.exists(cache_path):
                    self.cache.remove(cache_path)

                raise

        assert False, 'Should never get here'

    def _download(self, url, cache_path):
        import requests

        def copy_callback(read, total):
             if self.callback:
                self.callback('copy_file',read, total)


        if self.callback:
            self.callback('download', url, 0)

        if url.startswith('s3:'):

            from appurl.url import Url

            s3url = Url(url)

            try:
                with self.cache.open(cache_path, 'wb') as f:
                    s3url.object.download_fileobj(f)
            except Exception as e:
                raise DownloadError("Failed to fetch S3 url '{}': {}".format(url, e))

        elif url.startswith('ftp:'):
            from contextlib import closing

            with closing(urlopen(url)) as fin:

                with self.cache.open(cache_path, 'wb') as fout:

                    read_len = 16 * 1024
                    total_len = 0
                    while 1:
                        buf = fin.read(read_len)
                        if not buf:
                            break
                        fout.write(buf)
                        total_len += len(buf)

                        if self.callback:
                            copy_callback(len(buf), total_len)

        else:

            try:
                r = requests.get(url, stream=True)
                r.raise_for_status()
            except SSLError as e:
                raise DownloadError("Failed to GET {}: {} ".format(url, e))

            # Requests will auto decode gzip responses, but not when streaming. This following
            # monkey patch is recommended by a core developer at
            # https://github.com/kennethreitz/requests/issues/2155
            if r.headers.get('content-encoding') == 'gzip':
                r.raw.read = functools.partial(r.raw.read, decode_content=True)

            with self.cache.open(cache_path, 'wb') as f:
                copy_file_or_flo(r.raw, f, cb=copy_callback)

            assert self.cache.exists(cache_path)


class DelayedFlo(object):
    """Holds functions to open and close a file-like object"""

    def __init__(self, path, open_f, flo_f, close_f):
        self.path = path
        self.open_f = open_f
        self.flo_f = flo_f
        self.close_f = close_f
        self.memo = None
        self.message = None  # Set externally for debugging

    def open(self, mode):
        self.memo = self.open_f(mode)
        return self.flo_f(self.memo)

    def close(self):
        if self.memo:
            self.close_f(self.memo)



def get_dflo(url, syspath):
    """Return a Delayed FLO """
    import re
    import io
    from zipfile import ZipFile

    if url.is_archive:

        # Create a DelayedFlo for the file in a ZIP file. We might have to find the file first, though
        def _open(mode='r', encoding=None):
            zf = ZipFile(syspath)

            nl = list(zf.namelist())

            if url.target_file:
                # The archive file names can be regular expressions
                real_file_names = list([e for e in nl if re.search(url.target_file, e)
                                        and not (e.startswith('__') or e.startswith('.'))
                                        ])

                if real_file_names:
                    real_name = real_file_names[0]
                else:
                    raise SourceError("Didn't find target_file '{}' in  '{}' ".format(url.target_file, syspath))
            else:
                # No target file was specified, so use the first one.
                real_file_names = real_files_in_zf(zf)

                if real_file_names:
                    real_name = real_file_names[0]
                else:
                    raise SourceError("Can't find target file in '{}' ".format(url.target_file, syspath))

            if 'b' in mode:
                flo = zf.open(real_name, mode.replace('b', ''))
            else:
                flo = io.TextIOWrapper(zf.open(real_name, mode),
                                       encoding=url.encoding if url.encoding else 'utf8')

            return (zf, flo)

        def _close(f):
            f[1].close()
            f[0].close()

        df = DelayedFlo(syspath, _open, lambda m: m[1], _close)

    else:

        def _open(mode='rbU'):
            if 'b' in mode:
                return io.open(syspath, mode)
            else:
                return io.open(syspath, mode,
                               encoding=url.encoding if url.encoding else 'utf8')

        def _close(f):
            f.close()

        df = DelayedFlo(syspath, _open, lambda m: m, _close)

    return df

