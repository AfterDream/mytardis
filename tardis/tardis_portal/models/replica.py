from urlparse import urlparse

from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import models
from django.core.files.storage import default_storage
from django.utils import _os

from .datafile import Dataset_File

import logging
logger = logging.getLogger(__name__)

class Datafile_Replica(models.Model):
    """Class to store metadata about a physical file replica

    :attribute datafile: the foreign key for the 
       :class:`tardis.tardis_portal.models.Dataset_File` that this replica
       belongs to.
    :attribute url: the url that the replica is located at.
    :attribute protocol: the protocol used to access the replica.
    :attribute verified: if true, the replica has been verified as matching
       its digest in some point in the past.

    The `protocol` field is used for rendering the download link, this
    done by inserting the protocol into the url generated to the download
    location. If the `protocol` field is blank then the `file` protocol will
    be used.

    """

    datafile = models.ForeignKey(Dataset_File)
    url = models.CharField(max_length=400)
    protocol = models.CharField(blank=True, max_length=10)
    verified = models.BooleanField(default=False)

    class Meta:
        app_label = 'tardis_portal'

    def is_local(self):
        try:
            if self.protocol in (t[0] for t in settings.DOWNLOAD_PROVIDERS):
                return False
        except AttributeError:
            pass
        return urlparse(self.url).scheme == ''

    def get_actual_url(self):
        if self.is_local():
            # Local file
            return 'file://'+self.get_absolute_filepath()
        # Remote files are also easy
        url = urlparse(self.url)
        if url.scheme in ('http', 'https', 'ftp', 'file'):
            return self.url
        return None
    
    def _get_file(self):
        try:
            return self._get_file_getter()()
        except:
            return None
        
    def get_file_getter(self):
        """Return a function that will return a File-like handle for the 
           Replica's data.  The returned function uses a cached URL for 
           the replica to avoid depending on the current database transaction.
        """
        
        if not self.verified:
            return None
        return self._get_file_getter()

    def _get_file_getter(self):
        if self.is_local():
            theUrl = self.url
            def getter():
                return default_storage.open(theUrl)
            return getter
        else:
            theUrl = self.get_actual_url()
            def getter():
                return get_privileged_opener().open(theUrl)
            return getter

    def get_file(self):
        if not self.verified:
            return None
        return self._get_file()

    def get_absolute_filepath(self):
        url = urlparse(self.url)
        if url.scheme == '':
            try:
                # FILE_STORE_PATH must be set
                return _os.safe_join(settings.FILE_STORE_PATH, url.path)
            except AttributeError:
                return ''
        if url.scheme == 'file':
            return url.path
        # ok, it doesn't look like the file is stored locally
        else:
            return ''
            
    def deleteCompletely(self):
        import os
        filename = self.get_absolute_filepath()
        os.remove(filename)
        self.delete()

    def verify(self, df, tempfile=None, 
               allowEmptyChecksums=False, updateDatafile=False):
        '''
        Verifies this replica matches the datafile sizes and checksums. The 
        datafile must have at least one checksum hash to verify unless 
        "allowEmptyChecksums" is True.  If "updateDatafile" is True, the
        Datafile will be updated with missing sizes, checksums and an
        intuited mimetype.

        If passed a file handle, it will write the file to it instead of
        discarding the data as it's read.
        '''

        if not (allowEmptyChecksums or df.sha512sum or df.md5sum):
            return False

        def read_file(sf, tf):
            logger.info("Downloading %s for verification" % self.url)
            from contextlib import closing
            with closing(sf) as f:
                md5 = hashlib.new('md5')
                sha512 = hashlib.new('sha512')
                size = 0
                mimetype_buffer = ''
                for chunk in iter(lambda: f.read(32 * sha512.block_size), ''):
                    size += len(chunk)
                    if len(mimetype_buffer) < 8096: # Arbitrary memory limit
                        mimetype_buffer += chunk
                    md5.update(chunk)
                    sha512.update(chunk)
                    if tf:
                        tf.write(chunk)
                return (md5.hexdigest(),
                        sha512.hexdigest(),
                        size,
                        mimetype_buffer)

        sourcefile = self._get_file()
        if not sourcefile:
            return False
        md5sum, sha512sum, size, mimetype_buffer = read_file(sourcefile,
                                                             tempfile)

        if not (df.size and size == int(df.size)):
            if (df.sha512sum or df.md5sum) and not df.size: 
                # If the size is missing but we have a checksum to check
                # the missing size is harmless ... we will fill it in below.
                logger.warn("%s size is missing" % (self.url))
            else:
                logger.error("%s failed size check: %d != %s" %
                            (self.url, size, df.size))
                return False

        if df.sha512sum and sha512sum.lower() != df.sha512sum.lower():
            logger.error("%s failed SHA-512 sum check: %s != %s" %
                         (self.url, sha512sum, df.sha512sum))
            return False

        if df.md5sum and md5sum.lower() != df.md5sum.lower():
            logger.error("%s failed MD5 sum check: %s != %s" %
                         (self.url, md5sum, df.md5sum))
            return False

        if (updateDatafile):
            df.md5sum = md5sum.lower()
            df.sha512sum = sha512sum.lower()
            df.size = str(size)
            if not df.mimetype and len(mimetype_buffer) > 0:
                df.mimetype = Magic(mime=True).from_buffer(mimetype_buffer)

        self.verified = True
        self.save()

        logger.info("Saved %s for datafile #%d " % (self.url, self.id) +
                    "after successful verification")
        return True

